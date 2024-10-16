# impl from attn 2 paper by @erfanzar, (inspired by org impl by @Dao-AILab)
import functools
import math
from typing import Optional

import chex
import flax
import flax.linen
import flax.linen.attention
import jax
import triton
from fjformer.jax_triton import triton_call
from jax import custom_vjp
from jax import numpy as jnp
from jax import random as jrnd
from triton import language as tl
import numpy as np

FLASH_ATTN_BWD_ = True


def _simp_attn(
	query: chex.Array,
	key: chex.Array,
	value: chex.Array,
	bias: Optional[chex.Array],
	softmax_scale: float,
) -> chex.Array:
	"""Simplified attention function for testing and comparison.

	Args:
		query: Query array of shape (..., num_heads, seq_len_q, head_dim).
		key: Key array of shape (..., num_heads, seq_len_k, head_dim).
		value: Value array of shape (..., num_heads, seq_len_k, head_dim).
		bias: Optional bias array of shape (..., num_heads, seq_len_q, seq_len_k).
		softmax_scale: Scaling factor for the softmax function.

	Returns:
		Output array of shape (..., num_heads, seq_len_q, head_dim).
	"""
	dtype = query.dtype
	assert query.ndim == key.ndim, "q, k must have same rank."
	assert query.shape[:-3] == key.shape[:-3], "q, k batch dims must match."
	assert query.shape[-2] == key.shape[-2], "q, k num_heads must match."
	assert query.shape[-1] == key.shape[-1], "q, k depths must match."
	query = query * softmax_scale
	attn_weights = jnp.einsum("...qhd,...khd->...hqk", query, key)
	if bias is not None:
		attn_weights = attn_weights + bias
	attn_weights = jax.nn.softmax(attn_weights).astype(dtype)
	return jnp.einsum("...hqk,...khd->...qhd", attn_weights, value)


def get_strides(shape: tuple[int, ...]) -> tuple[int, ...]:
	"""Calculates strides for a given shape.

	Args:
		shape: Shape of the array.

	Returns:
		Tuple of strides.
	"""
	size = np.prod(shape)
	strides = []
	for s in shape:
		size = int(size // s)
		strides.append(size)
	return tuple(strides)


def get_sharding(arr: chex.Array):
	"""Gets the sharding of an array.

	Args:
		arr: Array to get sharding from.

	Returns:
		Sharding of the array.
	"""
	return getattr(arr, "sharding", None)


def check_shapes_and_dtypes(
	query: chex.Array,
	key: chex.Array,
	value: chex.Array,
	batch: int,
	seqlen_k: int,
	nheads: int,
	headdim: int,
	blocksize_k: int,
	blocksize_q: int,
):
	"""Checks the shapes and dtypes of the input arrays.

	Args:
		query: Query array.
		key: Key array.
		value: Value array.
		batch: Batch size.
		seqlen_k: Sequence length of the key.
		nheads: Number of heads.
		headdim: Head dimension.
		blocksize_k: Block size for the key.
		blocksize_q: Block size for the query.

	Raises:
		AssertionError: If the shapes or dtypes are not valid.
	"""
	chex.assert_shape(
		key,
		(batch, seqlen_k, nheads, headdim),
		custom_message="Shape mismatch for key.",
	)
	chex.assert_shape(
		value,
		(batch, seqlen_k, nheads, headdim),
		custom_message="Shape mismatch for value.",
	)
	chex.assert_equal(
		query.dtype, key.dtype, custom_message="Dtype mismatch between query and key."
	)
	chex.assert_equal(
		key.dtype, value.dtype, custom_message="Dtype mismatch between key and value."
	)
	if query.dtype not in [jnp.float16, jnp.bfloat16]:
		raise AssertionError("Only fp16 and bf16 are supported.") from None
	chex.assert_is_divisible(
		blocksize_k, 16, custom_message="blocksize_k should be divisible by 16."
	)
	chex.assert_is_divisible(
		blocksize_q, 16, custom_message="blocksize_q should be divisible by 16."
	)
	if headdim not in [16, 32, 64, 128, 256]:
		raise AssertionError("Unsupported headdim value.")


# fmt:off
@triton.heuristics(
	{
		"EVEN_M": lambda args: args["seqlen_q"] % args["BLOCK_M"] == 0,
		"EVEN_N": lambda args: args["seqlen_k"] % args["BLOCK_N"] == 0,
		"EVEN_HEADDIM": lambda args: args["headdim"] == args["BLOCK_HEADDIM"],
	}
)
@triton.jit
def _fwd_attn_kernel(
	Q,
	K,
	V,
	B,
	softmax_scale: float,
	stride_qb: int,
	stride_qh: int,
	stride_qm: int,
	stride_kb: int,
	stride_kh: int,
	stride_kn: int,
	stride_vb: int,
	stride_vh: int,
	stride_vn: int,
	stride_bb: int,
	stride_bh: int,
	stride_bm: int,
	stride_bn: int,
	stride_ob: int,
	stride_oh: int,
	stride_om: int,
	stride_lb: int,
	stride_lh: int,
	headdim: int,
	seqlen_q: int,
	seqlen_k: int,
	O,
	L,
	HAVE_BIAS: tl.constexpr,
	BLOCK_HEADDIM: tl.constexpr,
	EVEN_M: tl.constexpr,
	EVEN_N: tl.constexpr,
	EVEN_HEADDIM: tl.constexpr,
	BLOCK_M: tl.constexpr,
	BLOCK_N: tl.constexpr,
):
	"""Triton kernel for the forward pass of the attention mechanism.

	Args:
		Q: Query array.
		K: Key array.
		V: Value array.
		B: Bias array.
		softmax_scale: Scaling factor for the softmax function.
		stride_qb: Stride for the query batch dimension.
		stride_qh: Stride for the query head dimension.
		stride_qm: Stride for the query sequence dimension.
		stride_kb: Stride for the key batch dimension.
		stride_kh: Stride for the key head dimension.
		stride_kn: Stride for the key sequence dimension.
		stride_vb: Stride for the value batch dimension.
		stride_vh: Stride for the value head dimension.
		stride_vn: Stride for the value sequence dimension.
		stride_bb: Stride for the bias batch dimension.
		stride_bh: Stride for the bias head dimension.
		stride_bm: Stride for the bias query sequence dimension.
		stride_bn: Stride for the bias key sequence dimension.
		stride_ob: Stride for the output batch dimension.
		stride_oh: Stride for the output head dimension.
		stride_om: Stride for the output sequence dimension.
		stride_lb: Stride for the log-sum-exp batch dimension.
		stride_lh: Stride for the log-sum-exp head dimension.
		headdim: Head dimension.
		seqlen_q: Sequence length of the query.
		seqlen_k: Sequence length of the key.
		O: Output array.
		L: Log-sum-exp array.
		HAVE_BIAS: Whether bias is present.
		BLOCK_HEADDIM: Block size for the head dimension.
		EVEN_M: Whether the query sequence length is divisible by the block size.
		EVEN_N: Whether the key sequence length is divisible by the block size.
		EVEN_HEADDIM: Whether the head dimension is divisible by the block size.
		BLOCK_M: Block size for the query sequence dimension.
		BLOCK_N: Block size for the key sequence dimension.
	"""
	start_m, off_b, off_h = (
		tl.program_id(0),
		tl.program_id(1),
		tl.program_id(2),
	) #this one was the bug the whole time ...
	offs_m = start_m * BLOCK_M + tl.arange(0, BLOCK_M)
	offs_d = tl.arange(0, BLOCK_HEADDIM)
	offs_n = tl.arange(0, BLOCK_N)
	q_ptrs = Q + (off_b * stride_qb + off_h * stride_qh + (offs_m[:, None] * stride_qm + offs_d[None, :]))

	if EVEN_N & EVEN_M:
		if EVEN_HEADDIM:
			q = tl.load(q_ptrs)
		else:
			q = tl.load(q_ptrs, mask=offs_d[None, :] < headdim, other=0.0)
	else:
		if EVEN_HEADDIM:
			q = tl.load(q_ptrs, mask=offs_m[:, None] < seqlen_q, other=0.0)
		else:
			q = tl.load(q_ptrs, mask=(offs_m[:, None] < seqlen_q) & (offs_d[None, :] < headdim), other=0.0)
	k_ptrs = K + (off_b * stride_kb + off_h * stride_kh + (offs_n[:, None] * stride_kn + offs_d[None, :]))
	v_ptrs = V + (off_b * stride_vb + off_h * stride_vh + (offs_n[:, None] * stride_vn + offs_d[None, :]))
	softmax_scale = softmax_scale.to(tl.float32)

	if HAVE_BIAS:
		b_ptrs = B + (off_b * stride_bb + off_h * stride_bh + (offs_m[:, None] * stride_bm + offs_n[None, :]))

	lse_i = tl.zeros([BLOCK_M], dtype=tl.float32) - float("inf")
	max_i = tl.zeros([BLOCK_M], dtype=tl.float32) - float("inf")
	acc_o = tl.zeros([BLOCK_M, BLOCK_HEADDIM], dtype=tl.float32)

	for j in range(0, seqlen_k, BLOCK_N):
		j = tl.multiple_of(j, BLOCK_N)
		if EVEN_N:
			if EVEN_HEADDIM:
				k = tl.load(k_ptrs + j * stride_kn)
			else:
				k = tl.load(k_ptrs + j * stride_kn, mask=offs_d[None, :] < headdim, other=0.0)
		else:
			if EVEN_HEADDIM:
				k = tl.load(k_ptrs + j * stride_kn, mask=(j + offs_n)[:, None] < seqlen_k, other=0.0)
			else:
				k = tl.load(k_ptrs + j * stride_kn, mask=((j + offs_n)[:, None] < seqlen_k) & (offs_d[None, :] < headdim), other=0.0)
		qk = tl.zeros([BLOCK_M, BLOCK_N], dtype=tl.float32)
		qk += tl.dot(q, k.T)
		if not EVEN_N:
			qk += tl.where((j + offs_n)[None, :] < seqlen_k, 0, float("-inf")).to(tl.float32)
		if HAVE_BIAS:
			if EVEN_N & EVEN_M:
				b = tl.load(b_ptrs + j).to(tl.float32)
			else:
				b = tl.load(b_ptrs + j, mask=(offs_m[:, None] < seqlen_q) & (j + offs_n)[None, :] < seqlen_k, other=0.0).to(tl.float32)
			qk = (qk * softmax_scale) + b
			max_ij = tl.maximum(tl.max(qk, 1), lse_i)
			p = tl.exp(qk - max_ij[:, None])
		else:
			max_ij = tl.maximum(tl.max(qk, 1) * softmax_scale, lse_i)
			p = tl.exp(qk * softmax_scale - max_ij[:, None])

		l_ij = tl.sum(p, 1)
		acc_o_scale = tl.exp(max_i - max_ij)
		acc_o = acc_o * acc_o_scale[:, None]
		if EVEN_N:
			if EVEN_HEADDIM:
				v = tl.load(v_ptrs + j * stride_vn)
			else:
				v = tl.load(v_ptrs + j * stride_vn, mask=offs_d[None, :] < headdim, other=0.0)
		else:
			if EVEN_HEADDIM:
				v = tl.load(v_ptrs + j * stride_vn, mask=(j + offs_n)[:, None] < seqlen_k, other=0.0)
			else:
				v = tl.load(v_ptrs + j * stride_vn, mask=((j + offs_n)[:, None] < seqlen_k) & (offs_d[None, :] < headdim), other=0.0)

		acc_o += tl.dot(p.to(v.dtype), v)
		max_i = max_ij
		lin = tl.exp(lse_i - max_ij) + l_ij
		lse_i = max_ij + tl.log(lin)

	o_scale = tl.exp(max_i - lse_i)
	acc_o = acc_o * o_scale[:, None]
	lse_ptrs = L + (off_b * stride_lb + off_h * stride_lh + offs_m)
	tl.store(lse_ptrs, lse_i, mask=offs_m < seqlen_q)
	
	out_ptrs = O + (off_b * stride_ob + off_h * stride_oh + (offs_m[:, None] * stride_om + offs_d[None, :]))
	if EVEN_M:
		if EVEN_HEADDIM:
			tl.store(out_ptrs, acc_o)
		else:
			tl.store(out_ptrs, acc_o, mask=offs_d[None, :] < headdim)
	else:
		if EVEN_HEADDIM:
			tl.store(out_ptrs, acc_o, mask=offs_m[:, None] < seqlen_q)
		else:
			tl.store(out_ptrs, acc_o, mask=(offs_m[:, None] < seqlen_q) & (offs_d[None, :] < headdim))

# fmt:on


def _fwd_attn_kernel_call(
	query: Optional[chex.Array],
	key: Optional[chex.Array],
	value: Optional[chex.Array],
	bias: Optional[chex.Array] = None,
	softmax_scale: Optional[float] = None,
	blocksize_q: int = 128,
	blocksize_k: int = 128,
):
	"""Calls the Triton kernel for the forward pass of the attention mechanism.

	Args:
		query: Query array.
		key: Key array.
		value: Value array.
		bias: Bias array.
		softmax_scale: Scaling factor for the softmax function.
		blocksize_q: Block size for the query sequence dimension.
		blocksize_k: Block size for the key sequence dimension.

	Returns:
		Tuple of the output array and the log-sum-exp array.
	"""

	batch, seqlen_q, nheads, headdim = query.shape
	_, seqlen_k, _, _ = key.shape
	check_shapes_and_dtypes(
		query=query,
		key=key,
		value=value,
		batch=batch,
		seqlen_k=seqlen_k,
		nheads=nheads,
		headdim=headdim,
		blocksize_k=blocksize_k,
		blocksize_q=blocksize_q,
	)
	softmax_scale = softmax_scale or 1.0 / math.sqrt(headdim)
	HAVE_BIAS = True if bias is not None else False
	BLOCK_HEADDIM = max(triton.next_power_of_2(headdim), 16)
	stride_bb, stride_bh, stride_bm, stride_bn = (
		get_strides(bias.shape) if HAVE_BIAS else (0, 0, 0, 0)
	)
	stride_lb, stride_lh, stride_lm = get_strides((batch, nheads, seqlen_q))
	metaparams = dict(
		HAVE_BIAS=HAVE_BIAS,
		BLOCK_HEADDIM=BLOCK_HEADDIM,
		BLOCK_M=blocksize_q,
		BLOCK_N=blocksize_k,
	)

	stride_qb, stride_qm, stride_qh, stride_qd = get_strides(query.shape)
	stride_kb, stride_kn, stride_kh, stride_kd = get_strides(key.shape)
	stride_vb, stride_vn, stride_vh, stride_vd = get_strides(value.shape)

	num_warps = 4 if headdim <= 64 else 8
	return triton_call(
		query,
		key,
		value,
		bias if bias is not None else jnp.zeros((1,), jnp.float16),
		softmax_scale,
		stride_qb,
		stride_qh,
		stride_qm,
		stride_kb,
		stride_kh,
		stride_kn,
		stride_vb,
		stride_vh,
		stride_vn,
		stride_bb,
		stride_bh,
		stride_bm,
		stride_bn,
		stride_qb,
		stride_qh,
		stride_qm,
		stride_lb,
		stride_lh,
		headdim,
		seqlen_q,
		seqlen_k,
		kernel=_fwd_attn_kernel,
		out_shape=[
			jax.ShapeDtypeStruct(query.shape, query.dtype, sharding=get_sharding(query)),
			jax.ShapeDtypeStruct((batch, nheads, seqlen_q), jnp.float32),
		],
		grid=lambda META: (triton.cdiv(seqlen_q, META["BLOCK_M"]), batch, nheads),
		num_warps=num_warps,
		num_stages=1,
		name="triton::ops::_fwd_attn_kernel",
		**metaparams,
	)


@triton.jit
def _bwd_do_attn_kernel(
	O,
	Do,
	De,
	stride_ob: int,
	stride_om: int,
	stride_oh: int,
	stride_dob: int,
	stride_dom: int,
	stride_doh: int,
	stride_deb: int,
	stride_deh: int,
	nheads: int,
	headdim: int,
	seqlen_q: int,
	BLOCK_M: tl.constexpr,
	BLOCK_HEADDIM: tl.constexpr,
):
	"""Triton kernel for the backward pass of the attention mechanism with respect to the output gradient.

	Args:
		O: Output array.
		Do: Output gradient array.
		De: Delta array.
		stride_ob: Stride for the output batch dimension.
		stride_om: Stride for the output sequence dimension.
		stride_oh: Stride for the output head dimension.
		stride_dob: Stride for the output gradient batch dimension.
		stride_dom: Stride for the output gradient sequence dimension.
		stride_doh: Stride for the output gradient head dimension.
		stride_deb: Stride for the delta batch dimension.
		stride_deh: Stride for the delta head dimension.
		nheads: Number of heads.
		headdim: Head dimension.
		seqlen_q: Sequence length of the query.
		BLOCK_M: Block size for the query sequence dimension.
		BLOCK_HEADDIM: Block size for the head dimension.
	"""
	off_q = tl.program_id(0)
	off_hb = tl.program_id(1)
	off_b = off_hb // nheads
	off_h = off_hb % nheads
	offs_m = off_q * BLOCK_M + tl.arange(0, BLOCK_M)
	offs_d = tl.arange(0, BLOCK_HEADDIM)
	o_ptrs = (
		O
		+ off_b * stride_ob
		+ off_h * stride_oh
		+ offs_m[:, None] * stride_om
		+ offs_d[None, :]
	)
	do_ptrs = (
		Do
		+ off_b * stride_dob
		+ off_h * stride_doh
		+ offs_m[:, None] * stride_dom
		+ offs_d[None, :]
	)
	o = tl.load(
		o_ptrs,
		mask=(offs_m[:, None] < seqlen_q) & (offs_d[None, :] < headdim),
		other=0.0,
	).to(tl.float32)
	do = tl.load(
		do_ptrs,
		mask=(offs_m[:, None] < seqlen_q) & (offs_d[None, :] < headdim),
		other=0.0,
	).to(tl.float32)
	delta = tl.sum(o * do, axis=1)
	tl.store(
		De + (off_b * stride_deb + off_h * stride_deh + offs_m),
		delta,
		mask=offs_m < seqlen_q,
	)


@triton.heuristics(
	{
		"EVEN_M": lambda args: args["seqlen_q"] % args["BLOCK_M"] == 0,
		"EVEN_N": lambda args: args["seqlen_k"] % args["BLOCK_N"] == 0,
		"EVEN_HEADDIM": lambda args: args["headdim"] == args["BLOCK_HEADDIM"],
	}
)
@triton.jit
def _bwd_attn_kernel(
	Q,
	K,
	V,
	B,
	Do,
	L,
	D,
	softmax_scale: float,
	stride_qb: int,
	stride_qh: int,
	stride_qm: int,
	stride_kb: int,
	stride_kh: int,
	stride_kn: int,
	stride_vb: int,
	stride_vh: int,
	stride_vn: int,
	stride_bb: int,
	stride_bh: int,
	stride_bm: int,
	stride_dob: int,
	stride_doh: int,
	stride_dom: int,
	stride_dqb: int,
	stride_dqh: int,
	stride_dqm: int,
	stride_dkb: int,
	stride_dkh: int,
	stride_dkn: int,
	stride_dvb: int,
	stride_dvh: int,
	stride_dvn: int,
	stride_lb: int,
	stride_lh: int,
	seqlen_q: int,
	seqlen_k: int,
	headdim: int,
	nheads: int,
	Dq: chex.Array,
	Dk: chex.Array,
	Dv: chex.Array,
	HAVE_BIAS: tl.constexpr,
	BLOCK_HEADDIM: tl.constexpr,
	EVEN_M: tl.constexpr,
	EVEN_N: tl.constexpr,
	EVEN_HEADDIM: tl.constexpr,
	BLOCK_M: tl.constexpr,
	BLOCK_N: tl.constexpr,
):
	"""Triton kernel for the backward pass of the attention mechanism.

	Args:
		Q: Query array.
		K: Key array.
		V: Value array.
		B: Bias array.
		Do: Output gradient array.
		L: Log-sum-exp array.
		D: Delta array.
		softmax_scale: Scaling factor for the softmax function.
		stride_qb: Stride for the query batch dimension.
		stride_qh: Stride for the query head dimension.
		stride_qm: Stride for the query sequence dimension.
		stride_kb: Stride for the key batch dimension.
		stride_kh: Stride for the key head dimension.
		stride_kn: Stride for the key sequence dimension.
		stride_vb: Stride for the value batch dimension.
		stride_vh: Stride for the value head dimension.
		stride_vn: Stride for the value sequence dimension.
		stride_bb: Stride for the bias batch dimension.
		stride_bh: Stride for the bias head dimension.
		stride_bm: Stride for the bias query sequence dimension.
		stride_dob: Stride for the output gradient batch dimension.
		stride_doh: Stride for the output gradient head dimension.
		stride_dom: Stride for the output gradient sequence dimension.
		stride_dqb: Stride for the query gradient batch dimension.
		stride_dqh: Stride for the query gradient head dimension.
		stride_dqm: Stride for the query gradient sequence dimension.
		stride_dkb: Stride for the key gradient batch dimension.
		stride_dkh: Stride for the key gradient head dimension.
		stride_dkn: Stride for the key gradient sequence dimension.
		stride_dvb: Stride for the value gradient batch dimension.
		stride_dvh: Stride for the value gradient head dimension.
		stride_dvn: Stride for the value gradient sequence dimension.
		stride_lb: Stride for the log-sum-exp batch dimension.
		stride_lh: Stride for the log-sum-exp head dimension.
		seqlen_q: Sequence length of the query.
		seqlen_k: Sequence length of the key.
		headdim: Head dimension.
		nheads: Number of heads.
		Dq: Query gradient array.
		Dk: Key gradient array.
		Dv: Value gradient array.
		HAVE_BIAS: Whether bias is present.
		BLOCK_HEADDIM: Block size for the head dimension.
		EVEN_M: Whether the query sequence length is divisible by the block size.
		EVEN_N: Whether the key sequence length is divisible by the block size.
		EVEN_HEADDIM: Whether the head dimension is divisible by the block size.
		BLOCK_M: Block size for the query sequence dimension.
		BLOCK_N: Block size for the key sequence dimension.
	"""

	off_n, off_bh = (
		tl.program_id(0),
		tl.program_id(2),
	)
	off_h = off_bh % nheads
	off_b = off_bh // nheads
	offs_n = off_n * BLOCK_N + tl.arange(0, BLOCK_N)
	offs_d = tl.arange(0, BLOCK_HEADDIM)
	offs_m = tl.arange(0, BLOCK_M)

	q_ptrs = Q + (
		off_b * stride_qb
		+ (offs_m[:, None] * stride_qm)
		+ off_h * stride_qh
		+ offs_d[None, :]
	)
	k_ptrs = K + (
		off_b * stride_kb
		+ (offs_n[:, None] * stride_kn)
		+ off_h * stride_kh
		+ offs_d[None, :]
	)
	v_ptrs = V + (
		off_b * stride_vb
		+ (offs_n[:, None] * stride_vn)
		+ off_h * stride_vh
		+ offs_d[None, :]
	)
	if HAVE_BIAS:
		b_ptrs = B + (
			off_b * stride_bb
			+ off_h * stride_bh
			+ (offs_m[:, None] * stride_bm)
			+ offs_n[None, :]
		)
	dq_ptrs = Dq + (
		off_b * stride_dqb
		+ (offs_m[:, None] * stride_dqm)
		+ off_h * stride_dqh
		+ offs_d[None, :]
	)
	dk_ptrs = Dk + (
		off_b * stride_dkb
		+ (offs_n[:, None] * stride_dkn)
		+ off_h * stride_dkh
		+ offs_d[None, :]
	)
	dv_ptrs = Dv + (
		off_b * stride_dvb
		+ (offs_n[:, None] * stride_dvn)
		+ off_h * stride_dvh
		+ offs_d[None, :]
	)
	do_ptrs = Do + (
		off_b * stride_dob
		+ (offs_m[:, None] * stride_dom)
		+ off_h * stride_doh
		+ offs_d[None, :]
	)
	lse_ptrs = L + (off_b * stride_lb + off_h * stride_lh + offs_m)
	del_ptrs = D + (off_b * stride_lb + off_h * stride_lh + offs_m)

	k = tl.load(
		k_ptrs,
		mask=(offs_n[:, None] < seqlen_k) & (offs_d[None, :] < headdim),
		other=0.0,
	)
	v = tl.load(
		v_ptrs,
		mask=(offs_n[:, None] < seqlen_k) & (offs_d[None, :] < headdim),
		other=0.0,
	)

	dv = tl.zeros([BLOCK_N, BLOCK_HEADDIM], dtype=tl.float32)
	dk = tl.zeros([BLOCK_N, BLOCK_HEADDIM], dtype=tl.float32)

	for start_m in range(0, seqlen_q, BLOCK_M):
		start_m = tl.multiple_of(start_m, BLOCK_M)
		m_loop_offs = start_m + offs_m
		q = tl.load(
			q_ptrs + start_m,
			mask=(m_loop_offs[:, None] < seqlen_q) & (offs_d[None, :] < headdim),
			other=0.0,
		)

		qk = tl.dot(q, k.T)
		if not EVEN_N:
			qk += tl.where(offs_n[None, :] < seqlen_k, 0, float("-inf"))

		l = tl.load(lse_ptrs + start_m, mask=m_loop_offs < seqlen_q, other=0.0)[:, None]

		if HAVE_BIAS:
			b = tl.load(
				b_ptrs + start_m,
				mask=(m_loop_offs[:, None] < seqlen_q) & (offs_n[None, :] < seqlen_k),
				other=0.0,
			).to(tl.float32)
			qk = qk * softmax_scale + b
			p = tl.exp(qk - l)
		else:
			p = tl.exp(qk * softmax_scale - l)

		do = tl.load(
			do_ptrs + start_m,
			mask=(m_loop_offs[:, None] < seqlen_q) & (offs_d[None, :] < headdim),
			other=0.0,
		)

		dv = dv + tl.dot(p.to(do.dtype).T, do)
		dp = tl.dot(do, v.T)
		di = tl.load(del_ptrs + start_m, mask=m_loop_offs < seqlen_q, other=0.0)
		ds = (p * (dp - di[:, None]) * softmax_scale).to(q.dtype)
		foqi = tl.dot(ds.T, q).to(dk.dtype)
		dk += foqi

		dq = tl.dot(ds, k)
		pq = tl.load(
			dq_ptrs + start_m,
			mask=(m_loop_offs[:, None] < seqlen_q) & (offs_d[None, :] < headdim),
			other=0.0,
			eviction_policy="evict_last",
		)
		res = dq + pq
		tl.store(
			dq_ptrs + start_m,
			value=res,
			mask=(m_loop_offs[:, None] < seqlen_q) & (offs_d[None, :] < headdim),
			eviction_policy="evict_last",
		)

	tl.store(dv_ptrs, dv, mask=(offs_n[:, None] < seqlen_k) & (offs_d[None, :] < headdim))
	tl.store(dk_ptrs, dk, mask=(offs_n[:, None] < seqlen_k) & (offs_d[None, :] < headdim))


def _bwd_attn_kernel_call(
	softmax_scale: float,
	blocksize_q: int,
	blocksize_k: int,
	residual,
	Do: chex.Array,
):
	"""Calls the Triton kernel for the backward pass of the attention mechanism.

	Args:
		softmax_scale: Scaling factor for the softmax function.
		blocksize_q: Block size for the query sequence dimension.
		blocksize_k: Block size for the key sequence dimension.
		residual: Residual from the forward pass.
		Do: Output gradient array.

	Returns:
		Tuple of the gradients of the query, key, value, and bias arrays.
	"""
	(o, l, query, key, value, bias) = residual
	batch, seqlen_q, nheads, headdim = query.shape
	_, seqlen_k, _, _ = key.shape
	assert key.shape == (
		batch,
		seqlen_k,
		nheads,
		headdim,
	), "shape missmatch between key, value."
	assert value.shape == (
		batch,
		seqlen_k,
		nheads,
		headdim,
	), "shape missmatch between key, value."
	softmax_scale = softmax_scale or 1.0 / math.sqrt(headdim)
	if FLASH_ATTN_BWD_:
		assert headdim in {16, 32, 64, 128, 256}, "given headdim is not supported."
		assert query.dtype == key.dtype == value.dtype, "tensors must have the same dtype."
		assert query.dtype in [jnp.float16, jnp.bfloat16], "only support fp16 and bf16."
		HAVE_BIAS = True if bias is not None else False
		BLOCK_HEADDIM = max(triton.next_power_of_2(headdim), 16)

		stride_bb, stride_bh, stride_bm = (
			get_strides(bias.shape)[:-1] if HAVE_BIAS else (0, 0, 0)
		)
		stride_qb, stride_qm, stride_qh, stride_qd = get_strides(query.shape)
		stride_kb, stride_kn, stride_kh, stride_kd = get_strides(key.shape)
		stride_vb, stride_vn, stride_vh, stride_vd = get_strides(value.shape)
		stride_lb, stride_lh, stride_lm = get_strides(l.shape)
		stride_ob, stride_om, stride_oh, stride_od = get_strides(o.shape)
		delta = jnp.empty_like(l)
		stride_db, stride_dh, stride_dm = get_strides(delta.shape)

		num_warps = 4 if headdim <= 64 else 8

		# kernel kwargs
		metaparams = dict(
			BLOCK_M=blocksize_q,
			BLOCK_HEADDIM=BLOCK_HEADDIM,
			num_warps=num_warps,
			num_stages=1,
		)
		delta = triton_call(
			o,
			Do,
			delta,
			stride_qb,
			stride_qh,
			stride_qm,
			stride_qb,
			stride_qh,
			stride_qm,
			stride_db,
			stride_dh,
			nheads,
			headdim,
			seqlen_q,
			out_shape=[
				jax.ShapeDtypeStruct(
					shape=delta.shape,
					dtype=delta.dtype,
					sharding=delta.sharding,
				)
			],
			input_output_aliases={2: 0},
			grid=lambda META: (triton.cdiv(seqlen_q, META["BLOCK_M"]), batch, nheads),
			kernel=_bwd_do_attn_kernel,
			name="triton::ops::_bwd_do_attn_kernel",
			**metaparams,
		)[0]
		metaparams = dict(
			BLOCK_M=blocksize_q,
			BLOCK_N=blocksize_k,
			num_warps=num_warps,
			num_stages=1,
			BLOCK_HEADDIM=BLOCK_HEADDIM,
			HAVE_BIAS=HAVE_BIAS,
		)
		query_strides = (stride_qb, stride_qh, stride_qm)
		key_strides = (stride_kb, stride_kh, stride_kn)
		value_strides = (stride_vb, stride_vh, stride_vn)
		bias_strides = (stride_bb, stride_bh, stride_bm)
		d_output_strides = (stride_qb, stride_qh, stride_qm)
		d_query_strides = (stride_qb, stride_qh, stride_qm)
		d_key_strides = (stride_kb, stride_kh, stride_kn)
		d_value_strides = (stride_vb, stride_vh, stride_vn)
		lse_strides = (stride_lb, stride_lh)
		Dq, Dk, Dv = triton_call(
			query,
			key,
			value,
			bias if bias is not None else jnp.zeros((1,), jnp.float16),
			Do,
			l,
			delta,
			softmax_scale,
			*query_strides,
			*key_strides,
			*value_strides,
			*bias_strides,
			*d_output_strides,
			*d_query_strides,
			*d_key_strides,
			*d_value_strides,
			*lse_strides,
			seqlen_q,
			seqlen_k,
			headdim,
			nheads,
			kernel=_bwd_attn_kernel,
			grid=lambda META: (triton.cdiv(seqlen_k, META["BLOCK_N"]), 1, batch * nheads),
			out_shape=[
				jax.ShapeDtypeStruct(
					shape=query.shape, dtype=query.dtype, sharding=query.sharding
				),
				jax.ShapeDtypeStruct(shape=key.shape, dtype=key.dtype, sharding=key.sharding),
				jax.ShapeDtypeStruct(
					shape=value.shape, dtype=value.dtype, sharding=value.sharding
				),
			],
			name="triton::ops::_bwd_attn_kernel",
			**metaparams,
		)

		return Dq, Dk, Dv, None
	else:  # Flash attn bwd have some issue at the moment
		_, f_vjp = jax.vjp(
			functools.partial(_simp_attn, softmax_scale=softmax_scale),
			query,
			key,
			value,
			bias,
		)
		return f_vjp(Do)


def _fwd_attn_kernel_call_with_residual(
	query: Optional[chex.Array],
	key: Optional[chex.Array],
	value: Optional[chex.Array],
	bias: Optional[chex.Array] = None,
	softmax_scale: Optional[float] = None,
	blocksize_q: int = 128,
	blocksize_k: int = 128,
):
	"""Calls the Triton kernel for the forward pass of the attention mechanism and returns the residual.

	Args:
		query: Query array.
		key: Key array.
		value: Value array.
		bias: Bias array.
		softmax_scale: Scaling factor for the softmax function.
		blocksize_q: Block size for the query sequence dimension.
		blocksize_k: Block size for the key sequence dimension.

	Returns:
		Tuple of the output array and the residual.
	"""
	o, l = _fwd_attn_kernel_call(
		query=query,
		key=key,
		value=value,
		bias=bias,
		softmax_scale=softmax_scale,
		blocksize_k=blocksize_k,
		blocksize_q=blocksize_q,
	)
	return o, (o, l, query, key, value, bias)


@functools.partial(custom_vjp, nondiff_argnums=[4, 5, 6])
def _flash_attn2(
	query: chex.Array,
	key: chex.Array,
	value: chex.Array,
	bias: Optional[chex.Array] = None,
	softmax_scale: Optional[float] = None,
	blocksize_q: int = 128,
	blocksize_k: int = 128,
) -> chex.Array:
	"""Computes the attention mechanism using the Triton kernel.

	Args:
		query: Query array of shape (batch, seq_len_q, num_heads, head_dim).
		key: Key array of shape (batch, seq_len_k, num_heads, head_dim).
		value: Value array of shape (batch, seq_len_k, num_heads, head_dim).
		bias: Optional bias array of shape (batch, num_heads, seq_len_q, seq_len_k).
		softmax_scale: Scaling factor for the softmax function.
		blocksize_q: Block size for the query sequence dimension.
		blocksize_k: Block size for the key sequence dimension.

	Returns:
		Output array of shape (batch, seq_len_q, num_heads, head_dim).
	"""
	return _fwd_attn_kernel_call(
		query=query,
		key=key,
		value=value,
		bias=bias,
		softmax_scale=softmax_scale,
		blocksize_k=blocksize_k,
		blocksize_q=blocksize_q,
	)[0]


_flash_attn2.defvjp(
	_fwd_attn_kernel_call_with_residual,
	_bwd_attn_kernel_call,
)


def _test_forward():
	"""Tests the forward pass of the attention mechanism."""
	q_key, k_key, v_key = jrnd.split(jrnd.PRNGKey(8), 3)
	B, H, QS, KS, D = 1, 32, 1, 128_000, 128
	blocksize_k = 64
	blocksize_q = 128
	q = jax.nn.initializers.normal(2)(q_key, (B, QS, H, D), dtype=jnp.float16)
	k = jax.nn.initializers.normal(2)(k_key, (B, KS, H, D), dtype=jnp.float16)
	v = jax.nn.initializers.normal(2)(v_key, (B, KS, H, D), dtype=jnp.float16)
	b = (
		jnp.where(
			jrnd.randint(v_key, (B, H, QS, KS), 0, 4) > 2,
			jnp.finfo(jnp.float16).min,
			0,
		)
		if False
		else None
	)
	print("QKV Allocated")
	try:
		co = _flash_attn2(
			q, k, v, b, None, blocksize_k, blocksize_q
		)  # passes 256K on 24G GPU 3090
		print(co[-1, -1, -1, :5])
	except Exception as er:
		print("Flash OOM", er)
		co = None
	try:
		fo = flax.linen.attention.dot_product_attention(q, k, v, b)
		print(fo[-1, -1, -1, :5])
	except Exception as er:
		print("Flax OOM", er)
		fo = None
	if fo is not None and co is not None:
		print("Results are Close" if jnp.allclose(co, fo, 0, 0.125) else "Wrong results!")


def _test_backward():
	"""Tests the backward pass of the attention mechanism."""
	q_key, k_key, v_key = jrnd.split(jrnd.PRNGKey(8), 3)
	B, H, S, D = 1, 1, 64, 16
	blocksize_k = 64
	blocksize_q = 64
	q = jax.nn.initializers.normal(2)(q_key, (B, S, H, D), dtype=jnp.float16)
	k = jax.nn.initializers.normal(2)(k_key, (B, S, H, D), dtype=jnp.float16)
	v = jax.nn.initializers.normal(2)(v_key, (B, S, H, D), dtype=jnp.float16)

	b = (
		jnp.where(
			jrnd.randint(v_key, (B, H, S, S), 0, 4) > 2,
			jnp.finfo(jnp.float16).min,
			0,
		)
		if False  # Set to True to test with bias
		else None
	)

	try:
		co = jax.grad(lambda *x: _flash_attn2(*x, None, blocksize_q, blocksize_k).sum())(
			q, k, v, b
		)
		print("Custom op backward pass gradients:")
		print(co[-1][-1, -1, :5])  # Print last 5 elements of last head of last batch
	except Exception as er:
		print(f"Custom op backward pass failed: {er}")
		co = None

	try:
		fo = jax.grad(lambda *x: flax.linen.attention.dot_product_attention(*x).sum())(
			q, k, v, b
		)
		print("Flax backward pass gradients:")
		print(fo[-1][-1, -1, :5])  # Print last 5 elements of last head of last batch
	except Exception as e:
		print(f"Flax backward pass failed : {e}")
		fo = None
		exit()

	if fo is not None and co is not None:
		if jnp.allclose(co, fo, atol=0.125):
			print("Backward pass results are close.")
		else:
			print("Backward pass results differ significantly!")


triton_flash_attn_2_gpu = _flash_attn2
__all__ = ["triton_flash_attn_2_gpu"]

if __name__ == "__main__":
	# _test_forward()
	_test_backward()
