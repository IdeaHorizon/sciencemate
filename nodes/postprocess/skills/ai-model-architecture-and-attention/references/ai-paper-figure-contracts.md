# AI paper figure contracts

## Architecture diagrams

Minimum provenance:

- exact model/config identifier and immutable revision or content hash;
- ordered module topology;
- repeated-block counts;
- supplied tensor ranks and dimensions;
- declared residual, skip, branch, merge, conditioning, and shared-weight relations;
- a statement of whether the diagram is complete, abbreviated, or a selected subgraph.

Use editable SVG/PDF output. Arrows terminate at module boundaries. Do not let repeated-block notation imply a different execution order. A dimension label belongs to a tensor edge or port, not ambiguously between two modules. If topology or tensor semantics are missing, request them rather than drawing a plausible network.

### Backend decision table

| Architecture family | Primary rendering grammar | Semantic source | Publication requirement |
|---|---|---|---|
| CNN, AlexNet/VGG/ResNet, U-Net | PlotNeuralNet-derived TikZ tensor blocks | explicit config or traced graph | PDF plus SVG; tensor depth and skip paths visible |
| Transformer, BERT/T5/GPT, ViT | editable container/port SVG or Draw.io/TikZ | config plus normalized module IR | residual paths, repeated blocks, ports and tensor labels explicit |
| MoE, multimodal, diffusion conditioning, GNN | family-specific container/edge grammar | normalized module and edge semantics | routing/conditioning/message edges distinct from tensor flow |
| Full computation graph | torchview/FX/ONNX plus Graphviz | executed trace or exported graph | mark as diagnostic unless deliberately summarized |
| Conceptual AI illustration | generative image backend | prompt and provenance | raster illustration; never claim topology fidelity |

VisualTorch/Netron may inspect or preview a model, but their default output is not a publication layout. A VLM may review composition but may not certify topology. Deterministic comparison with the normalized architecture IR must check rendered module coverage, edge endpoints and semantics, repetition counts, supplied tensor dimensions, and absence of invented components.

Minimum normalized architecture IR:

- source framework, model/config identifier, immutable revision/hash, and extraction mode;
- module IDs, labels, types, hierarchy, repetition and optional collapsed groups;
- typed input/output ports and supplied tensor shapes;
- typed edges: tensor, residual, skip, cross-attention, conditioning, routing, or shared weight;
- completeness scope: full graph, overview, selected subgraph, or conceptual illustration;
- mapping from every rendered element to one or more source module/edge IDs.

## Attention matrices

Minimum provenance:

- checkpoint/model ID and revision;
- tokenizer ID and revision;
- exact input text or input hash;
- layer index and head index, or an explicit aggregation rule;
- query and key token labels, including special tokens;
- matrix shape and normalization axis;
- value range and whether dropout was active;
- any masking, averaging, subword aggregation, or token filtering.

Rows represent queries and columns represent keys unless explicitly declared otherwise. Attention probabilities use a sequential scale with a disclosed `[0, 1]` domain when they are true softmax probabilities. Do not use a diverging palette or re-normalize the matrix for visual contrast.

## Other model-derived figures

Feature maps, saliency, embeddings, confusion matrices, loss landscapes, diffusion denoising sequences, and graph-message-passing diagrams remain bound to their own quantitative or schematic contracts. This skill supplies AI semantics; it does not override the relevant quantitative integrity rules.
