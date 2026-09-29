---
name: ai-model-architecture-and-attention
description: Render publication-grade AI paper figures from supplied model topology, tensor semantics, checkpoints, configs, and derived activations, including neural-network architecture diagrams and attention matrices.
---

# AI Model Architecture and Attention

Treat model diagrams and model-derived matrices as scientific evidence, not decorative technology artwork.

For architecture figures, require structured modules and directed connections. Preserve layer order, repeated-block counts, tensor dimensions, residual or skip connections, branches, merges, and parameter sharing exactly as supplied. Do not infer omitted layers or tensor shapes from a model name.

Route by figure grammar instead of forcing every model through a generic flowchart:

- CNN, encoder-decoder, U-Net, and feature-map stacks: use the tensor-block TikZ backend;
- Transformer, LLM, ViT, MoE, multimodal, diffusion-conditioning, and GNN overviews: use the container/port architecture backend;
- literal execution or autograd graphs: use a traced Graphviz backend and label the result as a diagnostic graph;
- attention probabilities: use the quantitative attention renderer, with optional BertViz-style interactive HTML.

If the required specialist backend is unavailable, return `unsupported_backend` with the normalized architecture contract. Never fall back to the generic scientific-schematic renderer in publication mode, and never substitute a generated raster illustration for an evidence-bearing architecture diagram.

For attention figures, bind an actual layer, head, query axis, key axis, tokenizer, model revision, input hash, and normalization contract. Render the supplied attention probabilities as a quantitative heatmap. Never manufacture an attention pattern or present a conceptual heatmap as a model result.

Use concise labels and a restrained semantic palette. Repeated modules should use an explicit multiplier rather than visually duplicating dozens of identical blocks. Distinguish tensor flow, residual flow, and optional or training-only paths by declared edge semantics.

Load `references/ai-paper-figure-contracts.md` when the request includes a neural architecture, transformer, attention map, feature map, computational graph, residual network, graph neural network, or diffusion pipeline.
