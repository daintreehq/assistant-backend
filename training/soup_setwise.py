"""Average several stage-2 models into one: a uniform weight soup.

    python -m training.soup_setwise training/models/sw_a training/models/sw_b ... --out DIR

Averaging only means something between models that started from the same point
and stayed in one basin. The encoder always starts from the same pretrained
checkpoint; the head does only when every run was trained with the same
``--init-seed``. EXPERIMENTS.md records a soup of independently initialised
heads scoring below every ingredient ("heads unaligned across seeds"), so this
refuses ingredients whose init seeds are unset or differ, and ingredients that
disagree on anything the exporter reads from ``meta.json``.

The result is an ordinary stage-2 model directory that ``export_two_stage``
takes as ``--setwise``: one model's inference cost, whatever the ingredient count.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import torch

#: Keys that may differ between ingredients: they describe the run, not the model.
RUN_KEYS = {"seed", "routing_revision_at_train"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("ingredients", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    if len(args.ingredients) < 2:
        raise SystemExit("a soup needs at least two ingredients")
    if args.out.resolve() in {d.resolve() for d in args.ingredients}:
        raise SystemExit("--out is one of the ingredients; writing there would destroy it")

    metas = [json.loads((d / "meta.json").read_text()) for d in args.ingredients]
    if metas[0].get("init_seed") is None:
        raise SystemExit("ingredients were not trained with --init-seed; their heads do not align")
    for d, m in zip(args.ingredients[1:], metas[1:], strict=True):
        diff = sorted(
            k for k in set(m) | set(metas[0])
            if k not in RUN_KEYS and m.get(k) != metas[0].get(k)
        )
        if diff:
            raise SystemExit(f"{d} differs from {args.ingredients[0]} on {diff}")

    states = [torch.load(d / "model.pt", map_location="cpu") for d in args.ingredients]
    soup: dict[str, dict[str, torch.Tensor]] = {}
    for part in ("backbone", "head"):
        soup[part] = {}
        for key, first in states[0][part].items():
            if first.is_floating_point():
                stacked = torch.stack([s[part][key].float() for s in states])
                soup[part][key] = stacked.mean(0).to(first.dtype)
            else:
                # Integer buffers (position ids and the like) are not weights.
                soup[part][key] = first.clone()

    args.out.mkdir(parents=True, exist_ok=True)
    torch.save(soup, args.out / "model.pt")
    src = args.ingredients[0]
    for name in src.iterdir():
        if name.name not in ("model.pt", "meta.json") and name.is_file():
            shutil.copy2(name, args.out / name.name)
    meta = dict(metas[0])
    meta["seed"] = [m["seed"] for m in metas]
    meta["soup_of"] = [str(d) for d in args.ingredients]
    (args.out / "meta.json").write_text(json.dumps(meta, indent=2))
    print(f"souped {len(states)} models into {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
