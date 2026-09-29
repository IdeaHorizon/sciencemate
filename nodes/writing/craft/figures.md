# 图：向图表服务提需求

你不画图。你说清每张图要让读者看出什么，图表服务负责画、审、铸记录。调用形式：

```
request_figures(requests=[
  {"intent": "让读者看出：拓扑 6 在四种模型上 wall time 均最低，拓扑 1 最高",
   "asset_kind": "quantitative",
   "source_artifact_ids": ["dataset__xxx"],
   "constraints": {"width": "double_column",
                   "panels": "2x2 by network, x=TP/EP strategy, stacked GPU+Comm"}},
  {"intent": "让读者一眼看懂拓扑 1 的结构：32 台单 CPU 单 GPU 单 NIC 服务器星型接一台 400Gbps 交换机",
   "asset_kind": "schematic",
   "constraints": {"width": "single_column"},
   "spec": {"nodes": [{"id": "sw", "label": "External switch (400 Gbps)"},
                      {"id": "srv", "label": "Server x32: 1 CPU, 1 GPU, 1 NIC"}],
            "edges": [{"source": "srv", "target": "sw", "label": "NIC"}]}}
])
```

语言政策、印刷宽度（单栏 84 mm / 双栏 170 mm）、字号下限 7 pt、pdf+png、用户的禁用词与重命名映射，
由 request_figures 按简报自动带上，不用你写、也改不掉。

## 规则
- 一张图一个信息，写进 intent；图题按同一个信息写。
- 数据图的数据来自卷宗里的数据表 artifact（source_artifact_ids 填它的 id），不自己抄数。
- 印刷宽度：单栏 84 mm，双栏 170 mm；轴标签与图例在印刷尺寸下不小于 7 pt。多面板图最多 4 个面板（2×2），每个面板要单独可读；不要把 6 张或 8 张小图挤进一张，那种图在页面上会缩成看不清的小字。
- 图的长宽比要横向或接近方形（宽:高 ≥ 1:1.2），在 constraints 里写 aspect: "landscape"。竖着堆面板的长条图（宽:高 1:3）放进页面会超出版心、把图题挤出页外，或被缩到看不清；六种拓扑要一张总览就 3×2 横排，或者每种一张。
- 结构示意图（拓扑、流程）每种一张，各自一个 figure 请求；六种拓扑就是六张图，不做合成图。
- 数据总览类的图按模型或按条件拆成多张，每张 2×2 以内。
- 数据图可以一次交齐；示意图每次最多 3 张一批（图表服务画示意图要先立合同再渲染，一批太多它会在回合上限前一张都交不出来）。图表服务返回后用 read_dossier(part='figures', rebuild=true) 看新文件，再更新提纲里的 source，再起草结果节。
- 不要让图表服务去转换或修改用户交来的图文件；要新画，就按描述新画。
- 图没画出来或画错了，带着具体缺陷再派一次，不要将就用旧图，也不要在正文里解释图的问题。
- 拓扑示意图这类结构图用 asset_kind=schematic 加 spec（节点与边），不要让服务猜结构。
