"""The set-wise selection head — stage 2's architecture, shared by train and export.

## The shape of the computation

    conversation  ──► bge-base encoder ──► q          (768-d, per request)
    skill i       ──► bge-base encoder ──► s_i        (768-d, CACHED at build)

    token_0 = Wq(q)                                   ← the CLS slot
    token_i = Wc([ s_i ; q⊙s_i ; |q−s_i| ]) + Wf(feats_i)

    [token_0 … token_K] ──► 2 transformer layers ──► per-slot logits
                                                     + docs / cardinality from slot 0

The encoder runs ONCE per turn, on the conversation only. Skill vectors are read
from a table. So the request-time cost of a candidate is a 768-float lookup and
one row of attention — not a forward pass, which is exactly what made the
abandoned cross-encoder reranker (``EXPERIMENTS.md``, Architecture A) cost 309ms
of a 312ms pipeline.

## Why a transformer over the candidates rather than a dot product

A dot product scores each skill in isolation, so it cannot express the one thing
the residual errors are actually about: comparison. "Both of these look like
worktree work, but THAT one owns the case where agents are already running" is a
statement about a *pair* of candidates, and cardinality ("does this turn need one
of these or none of them?") is a statement about the whole set. Self-attention
across the candidate slots gives the head a place to compute both, and reading
docs/cardinality off the CLS slot means they see the candidate set too, not just
the query.

## Why there is no positional encoding by default

The head must generalise to skills it never saw in training — that is the whole
point of consuming a skill EMBEDDING rather than owning a per-skill weight row.
With no positional term the module is permutation-equivariant: the answer for a
candidate depends on its content and its features, never on which slot it landed
in. That makes the shortlist's ordering irrelevant by construction, which is the
safest possible answer to "does the retrieval order bias the head".

Retrieval order is not thrown away — it enters as an explicit per-candidate
FEATURE (the cosine score, and optionally the normalised rank), where the head
can weigh it like any other evidence instead of inheriting it as a prior on slot
0. ``rank_positions`` enables the order-sensitive alternative so the two can be
measured against each other rather than argued about.
"""

from __future__ import annotations

import torch
from torch import nn

# Imported from the SHIPPED package, not redefined here. The serving code builds
# this feature vector and the trainer builds the targets it is learned against;
# as two equal literals with nothing checking they stayed equal, reordering one
# would train one meaning and serve another, silently and with no error
# anywhere. Same reason ``documents.py`` owns the text rendering: training
# follows serving.
from daintree_assistant_server.skills.local.setwise import FEATURE_NAMES

__all__ = ["CARD_CLASSES", "FEATURE_NAMES", "RetrieverGraph", "SetwiseGraph", "SetwiseHead"]

#: Cardinality classes: 0, 1, 2, 3-or-more skills.
CARD_CLASSES = 4


class SetwiseHead(nn.Module):
    """Scores a whole candidate SET at once, from cached embeddings."""

    def __init__(
        self,
        hidden_size: int,
        *,
        d_model: int = 256,
        layers: int = 2,
        heads: int = 4,
        dropout: float = 0.1,
        rank_positions: int = 0,
    ) -> None:
        super().__init__()
        self.d_model = d_model
        self.rank_positions = rank_positions
        self.q_proj = nn.Linear(hidden_size, d_model)
        self.cand_proj = nn.Linear(hidden_size * 3, d_model)
        self.feat_proj = nn.Linear(len(FEATURE_NAMES), d_model)
        self.rank_emb = (
            nn.Embedding(rank_positions, d_model) if rank_positions > 0 else None
        )
        self.norm_in = nn.LayerNorm(d_model)
        layer = nn.TransformerEncoderLayer(
            d_model=d_model,
            nhead=heads,
            dim_feedforward=d_model * 4,
            dropout=dropout,
            activation="gelu",
            batch_first=True,
            norm_first=True,
        )
        # ``enable_nested_tensor`` off: the fast path silently changes the
        # module's traced graph and does not survive ONNX export, which is the
        # only form this head ever runs in production.
        self.encoder = nn.TransformerEncoder(
            layer, num_layers=layers, enable_nested_tensor=False
        )
        self.skill_out = nn.Linear(d_model, 1)
        self.docs_out = nn.Linear(d_model, 1)
        self.card_out = nn.Linear(d_model, CARD_CLASSES)

    def forward(
        self,
        q: torch.Tensor,
        s: torch.Tensor,
        feats: torch.Tensor,
        mask: torch.Tensor,
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """Score one batch of candidate sets.

        ``q``     (B, H)     pooled conversation embedding
        ``s``     (B, K, H)  pooled skill embeddings for the shortlist
        ``feats`` (B, K, F)  per-candidate scalars (see ``FEATURE_NAMES``)
        ``mask``  (B, K)     1 for a real candidate, 0 for padding

        Returns per-candidate skill logits (B, K), docs logits (B,) and
        cardinality logits (B, CARD_CLASSES).

        ``K`` is FIXED at export time and serving always supplies exactly that
        many slots, padding with zeros and ``mask=0`` when the catalog is
        smaller. This is not a simplification — ``nn.MultiheadAttention`` traces
        to reshapes with the sequence length baked in, so a graph exported with a
        "dynamic" candidate axis loads happily and then fails at run time on any
        other K. Padding to a static shape is both the correct answer and the
        faster one: onnxruntime optimises a fully static graph harder.
        """
        q_expanded = q.unsqueeze(1).expand_as(s)
        cand = self.cand_proj(
            torch.cat([s, q_expanded * s, (q_expanded - s).abs()], dim=-1)
        )
        cand = cand + self.feat_proj(feats)
        if self.rank_emb is not None:
            positions = torch.arange(s.shape[1], device=s.device).clamp(
                max=self.rank_positions - 1
            )
            cand = cand + self.rank_emb(positions).unsqueeze(0)

        cls = self.q_proj(q).unsqueeze(1)
        tokens = self.norm_in(torch.cat([cls, cand], dim=1))
        # The CLS slot is always valid; padded candidate slots are masked out of
        # attention so a half-full shortlist cannot leak zeros into the pooled
        # judgement (the catalog is smaller than K on a small deploy, and the
        # last shard of any batch is ragged).
        pad = torch.cat(
            [torch.zeros_like(mask[:, :1], dtype=torch.bool), mask == 0], dim=1
        )
        encoded = self.encoder(tokens, src_key_padding_mask=pad)

        skill_logits = self.skill_out(encoded[:, 1:]).squeeze(-1)
        # Padding slots must never win a slot in the ranking downstream.
        skill_logits = skill_logits.masked_fill(mask == 0, -1e4)
        pooled = encoded[:, 0]
        return skill_logits, self.docs_out(pooled).squeeze(-1), self.card_out(pooled)


class SetwiseGraph(nn.Module):
    """Encoder + mean-pool, folded into one exportable graph.

    Pooling lives inside the graph for the same reason it does in the shipped
    classifier's export: the serving code cannot pool differently from the
    trainer if it does not pool at all. This graph is run on TWO kinds of text —
    conversations at request time, skill documents at build time — with the same
    weights, which is what lets a skill added after the build be embedded into
    the same space without retraining anything.
    """

    def __init__(self, backbone: nn.Module) -> None:
        super().__init__()
        self.backbone = backbone

    def forward(
        self, input_ids: torch.Tensor, attention_mask: torch.Tensor
    ) -> torch.Tensor:
        hidden = self.backbone(input_ids=input_ids, attention_mask=attention_mask)[0]
        m = attention_mask.unsqueeze(-1).to(hidden.dtype)
        return (hidden * m).sum(1) / m.sum(1).clamp(min=1e-9)


class RetrieverGraph(nn.Module):
    """Stage 1's encoder: mean-pool then L2-normalise, so serving does a plain dot."""

    def __init__(self, backbone: nn.Module) -> None:
        super().__init__()
        self.backbone = backbone

    def forward(
        self, input_ids: torch.Tensor, attention_mask: torch.Tensor
    ) -> torch.Tensor:
        hidden = self.backbone(input_ids=input_ids, attention_mask=attention_mask)[0]
        m = attention_mask.unsqueeze(-1).to(hidden.dtype)
        pooled = (hidden * m).sum(1) / m.sum(1).clamp(min=1e-9)
        return torch.nn.functional.normalize(pooled, dim=-1)
