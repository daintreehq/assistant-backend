# Third-party notices

The repository's MIT licence covers Daintree's original code, runbooks and
synthetic examples. Dependencies and base models retain their own licences.

The selector metadata names these pretrained models, which are fine-tuned and
exported to ONNX by the training pipeline:

| Component | Upstream model | Licence |
| --- | --- | --- |
| Ranking encoder | [BAAI/bge-base-en-v1.5](https://huggingface.co/BAAI/bge-base-en-v1.5) | MIT |
| Retriever encoder | [sentence-transformers/all-MiniLM-L6-v2](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2) | Apache 2.0 |

The model binaries are downloaded separately using `selector.lock`; they are not
tracked by Git. Modified model exports are produced by the scripts in `training/`.
When redistributing those artifacts, retain their upstream licence and attribution
notices. Copies of the upstream licences are in `third_party/`.

Python dependencies are declared in `pyproject.toml`, pinned in
`requirements.lock`, and installed with their own distribution metadata. Training
dependencies are separate in `training-requirements.txt` and
`training/requirements-train.lock`.
