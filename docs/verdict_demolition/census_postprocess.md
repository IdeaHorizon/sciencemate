# 普查：nodes/postprocess/（2026-08-31）

计数：804 拒绝点（raise 763 + return-error 41）→ A=17 / B=21 / C=520 / **D=246**。
D 分布：rendering.py 96、relational_grammar.py 48、lifecycle.py 32、observation_rendering 15、
spatial 10、planning 10、geospatial 7、reviewer 5、trajectory 5、electronic_structure 3、
reviewer_benchmark 3、其余各 2。

## D 类逐条（裁决对象）

### tools/lifecycle.py（32）
162 不许把不支持的 request 换成另一个做 ｜ 179 visual_request 与调用方契约不可变字段有差即拒 ｜
194/200/217/228 组件图 plan hash/源 hash/文件 hash/figure_hash 过期必须重做（血缘闸四条） ｜
372 source profile blocked_upstream 就不许规划 ｜ 393 design profile 必须先 design_decision 才能 plan ｜
431 design_decision 必须带 1..1200 字 rationale ｜ 438 design profile 要求显式 chart_type ｜
448/506/535 design_decision 不得覆盖调用方 preferred_form/encoding role/style_direction ｜
515 必须为每个必需视觉角色显式绑定字段 ｜ 551/556 rejected_alternative 必须不同 form 带理由/至少一个被否方案 ｜
680 修订必须写非空 reason ｜ 691 只允许表现层修订键 ｜ 698 based_on_review_id 必须评当前版 plan ｜
721/728/745 final_size_mm 宽/高/formats 被调用方或出版 profile 锁死 ｜ 793 spatial_camera_zoom∈[0.5,1.0] ｜
908/912 visual_derived_data 为另一版 plan 产的/输入 hash 过期即拒复用 ｜ 1040 合成图高超出版 profile 上限 ｜
1399 校验报告对这张图已过期 ｜ **1401 机械校验必须先 pass 才能进 VLM 审图（顺序闸，30 天开火 27 次）** ｜
1965 figure_package 质量闸未过 finalize 返回 error ｜ 2439 有界修订产出与上版逐字节相同禁止再送审 ｜
2480 生命周期末端质量闸未过整链 error ｜ 2543 必须先 prepare_requested_visual_design 才能 create（顺序闸）

### v2/rendering.py（96）——display-ready 契约闸
特征标注（190-258）：contract 必须逐字声明 kind/coordinate_system/provenance；数量 1..12；旋转只准 0/90；
偏移 [2,12]pt；文字 ≤48 字符；feature_kind 必须属内置词表；必须精确落在上游轨迹坐标上；不得重复。
语义标注（323-369）：恰好一个响应字段；provenance 必须 upstream_confirmed；selection_policy 只准
nonempty_label_rows；max_labels∈[1,12]；每行完整坐标；超 max_labels 即拒。
统计图完整性（578-1973）：直方图 bin 行不完整/重叠即拒（"省略是禁止的"）；box 五数摘要顺序必须
科学有效、行不完整即拒；violin 密度必须完整且 value 严格递增；相关矩阵必须方阵/对角线等于声明值/
严格对称；热图超声明 limits；频谱要求完整矩形时频网格（STFT/插值必须上游做）+自证 kind+三标签+
线性谱值非负+limits；向量场行完整+声明单位；contour 完整矩形网格+显式层级；三元行完整非负+
和等于声明值（归一化上游做）；manhattan 必须声明坐标类型+染色体序不重不漏+每染色体有点+区间不相交；
富集图四标签+点标签唯一；event raster 必须显式 trial_order 不重不漏；ridge 行完整密度非负；
森林图区间 lower≤est≤upper 且完整；柱状图/配对图重复类别行即拒（聚合上游做）；行坐标完整；
ordination 自证 PCA 类 kind+双标签+PCA 方差 [0,1] 和≤1；色谱 x 唯一严格递增+自证保留时间+分离方法+
信号类型；衍射自证 kind+辐射源+强度非负；生存曲线无缺失概率+区间完整+[0,1]+每序列非增+自证估计量；
诊断曲线 kind 一致+显式 x/y range+坐标不越 range+概率轴 [0,1]+ROC 非降+校准 x 唯一递增+
reference 显式对角线+volcano 显著性有限非负+MA 数值 center_line+QQ 分位递增/非降+残差数值 reference；
Bland-Altman 上游给 ≥3 参考线。
空间/分子（2183-3511）：特征标注要求轨迹非常数；HDR 必须显式分类 LUT+display_window（自动 min-max
禁止）+contrast∈[0.25,4]；分子投影必须上游 coordinate_system；多帧必须显式 frame_index（"不替你猜"）；
show_cell 要求有限非退化晶胞；键 contract 必须给 index_base+唯一+order 正；radius_scale∈[0.25,2]；
schematic 节点 status/边类型必须属内置词表。

### v2/relational_grammar.py（48）——关系图语法闸
39 标签 ≤80 ｜ 63 契约必须给非空语义/单位/轴标签（10 调用点） ｜
**71 契约字段必须逐字等于指定值（provenance="upstream_computed"/coordinate_system="normalized_layout"/
rooted=True 等，22 个调用点——框架不验真伪的逐字断言闸）** ｜
111 network kind 词表 ｜ 166 边权严格正 ｜ 215 ≤40 节点 120 边 ｜ 221 节点标签唯一 ｜ 224 group 覆盖每节点 ｜
226 ≤6 分组 ｜ 258/262 流量区间不重叠无未声明空隙 ｜ 280 flow kind 一致 ｜ 292 conservation_policy 三值 ｜
301 不守恒必须写 rationale ｜ 319 stage x 严格递增 ｜ 321 ≤6 stage ｜ 379 流严格向前 ｜ 383 端点对齐声明 stage ｜
390 ribbon 厚度精确等于 value*scale ｜ 447 ≤32 节点 80 ribbon ｜ 458 声明 stages 与观测完全一致 ｜
464 流节点标签唯一 ｜ 488/496/500 守恒几何一致/不过即拒/例外只在声明策略下 ｜ 511 ≤6 流分组 ｜
560/581/586 恰好声明根无断开/树不断开/内部节点 ≥2 子 ｜ 602/617 leaf_order 不重不漏/与上游 y 单调一致 ｜
689 父高严格大于子高 ｜ 712 出版尺寸 ≤40 叶 ｜ 718/720/725 每叶有标签/只准叶标签/唯一 ｜
729 内部节点 y 落在子间 ｜ 768 phylogeny kind 词表 ｜ 812 根距 0≤parent<child ｜ 856 ≤40 tip 80 节点 ｜
863/865/870 每 tip 有标签/只准 tip 身份标签/唯一 ｜ 874 内部 y 落在子间 ｜ 885/893 支持度数值 range/落在 range 内 ｜
908/910 tip group 覆盖每 tip/≤6 分组

### v2/observation_rendering.py（15）
29 受控词表不重不漏覆盖观测值 ｜ 63 timeline 必须 time_label ｜ 85 时间轴标签 1-64 字符 ｜
215 矩阵 verdict 受控词表+strength∈[0,1] ｜ 221 矩阵矩形+缺失显式 missing ｜ 308 堆叠柱无重复行 ｜
314 成分表矩形或显式声明补零 ｜ 323 share 每类和为 1 ｜ 393 diverging 上游给带符号差+reference=0 ｜
418 每 group 覆盖全部 label ｜ 446 两侧有值或显式 allow_one_sided ｜ 486 radar 上游归一化+方向一致 ｜
496 radar 3..12 维 ｜ 513 每 group 覆盖全轴 ｜ 576 图型必须有对应上游语义契约

### v2/spatial_rendering.py（10）
240 framing_zoom∈[0.5,1.0] ｜ 263 必须上游 coordinate_system ｜ 306 每字段必须上游单位或显式声明 ｜
466/559 瞬态必须显式 time_index（不替你选时刻） ｜ 598 多块必须显式 block 选择 ｜ 715 seed_plane 分辨率 [2,100] ｜
737 seed_line [2,500] ｜ 961 slice_count∈[2,40] ｜ 1300 pure 模式不满足纯矢量判据整体拒绝不许降级

### v2/planning.py（10）
338 区间字段但上游无 uncertainty_contract → 必须 Experiment 先声明 ｜ 355/357 森林图必须有 uncertainty
contract/上游 reference_value ｜ 372 单位不兼容不许同轴 ｜ 478 图型必须有指定上游语义契约 ｜
486 reviewed 热图必须完整 matrix_contract ｜ 580 图高超出版上限 ｜ 635 语义布局超高要求拆图 ｜
725 生成式插图必须先 generative_allowed=true 授权 ｜ 738 带 nodes/edges 不许走生成式后端

### v2/geospatial_rendering.py（7）
94 必须 geospatial_contract ｜ 97 coordinate_variables 显式绑定经纬 ｜ 145 多值维度显式 index ｜
177 field_units 必须声明单位 ｜ 247 矢量必须已 destagger ｜ 255 u/v 同显式单位 ｜ 266 箭头 >2500 拒绝要求加 stride

### v2/reviewer.py（5）
233 观察 ≤8 条 ｜ 249 观察不许出现 confidence/severity/verdict 等判决字段 ｜ 269 不许写"无缺陷"式观察 ｜
318 不许评判科学数据分布 ｜ 335 全干净必须返回空数组

### v2/trajectory_rendering.py（5）
52 必须 trajectory_contract ｜ 58 必须 coordinate_system+length_unit ｜ 96 多帧显式 frame_index ｜
106 显式 atom_selection（连 all 也要写） ｜ 139 max_rendered_atoms∈[100,500000]

### 其余（13）
electronic_structure 212/217/223 必须 contract/energy_unit/scientific_format ｜
reviewer_benchmark 49/63/91 基准 ≥20 用例/缺陷例必须 gold region+规则引用/字体缺失即拒 ｜
mcp_adapters 155/166 必须自证确定性渲染/离屏冒烟必须过 ｜ imagegen_http 130/134 generative_role 词表/
结构化示意必须走确定性渲染 ｜ spatial_vector_export 178/186 图元超上限拒绝要求混合导出 ｜
composite 227/234 合成尺寸过小/面板太矮放不下可读标签 ｜ schematic_layout 36/44 坐标全给或全不给/节点不同位 ｜
label_layout 44/167 语义标签 1..24/找不到无碰撞位置整体拒绝

## 骑墙判例
1. rendering.py:578 直方图 bin 不完整即拒 → D（能画出完整那部分，如实记"丢 N 行"账真可逆）。
2. mcp_adapters.py:241 scientific_values_changed 非 false 即拒 → B（放行则图谎称代表源数据）。
3. planning.py:734/imagegen_http.py:132 evidence_bearing 必须 false → B（生成像素与证据的出处墙）；
   同函数 725 generative_allowed 授权闸 → D。
4. reviewer.py:249 审图观察禁带判决字段 → D（角色分工，多记一个意见账仍真）。
5. relational_grammar.py:71 逐字断言闸 → D（要求抄写固定值而不验真伪，纯仪式，一点改动波及四套语法）。
