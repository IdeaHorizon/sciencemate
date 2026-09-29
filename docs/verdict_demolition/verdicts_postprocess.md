# 判决书：nodes/postprocess/（250 条；删 73 / 降格 171 / 保留升格 4 / 呈裁 2 + 3 条条件/前置项）

## 判决表（要点）

### lifecycle.py（32：删 12 / 降格 16 / 保留 2 / 呈裁 2）
- **1401 删**（机械校验先于 VLM：27 次开火 10 次 run 结束 8 次 report_blocker 零合规——
  fire_data 死路铁证）：validation 报告（含失败）随图进 VLM 上下文，审图记录写
  mechanical_validation_passed:false。
- **2543 删**（prepare 先于 create：2292 行已有完整 None 分支自建 brief/profile，拒绝纯仪式）。
- **1965/2480 删**（部分成功判全废：1941-1955 已把 package 存盘并如实写 package_status/problems，
  再判 error 零信息增量）→ OB-VALIDATION-OPEN。
- 393/438/515/551/556 删（design 事前审批链+被否方案仪式）；431/680 删（字数闸）；793 删（zoom 阈值）。
- 162/179/448/506/535/728/745 降格→OB-DEVIATION（保护调用方契约的探测真，执行违 S4：
  declared_deviation{requested,delivered,reason}）。
- 194/200/228/908/912/1399/698 降格→OB-LINEAGE（过期=义务「重做或声明沿用理由」+合成图
  **逐面板披露血缘版本**）。
- 372→OB-CONTRACT-META；1040→OB-PUBSPEC；691 改判 C（表现层键=工具类型边界，指路具体）。
- **2439 升 A**：实现已是降格形态（不 raise，记 progress 后回退上一版继续）——逐字节同 PNG
  再送 VLM=纯算力空转；仅把 "error" 措辞改 no_effective_change。
- **217 呈裁**（文件被外部改写≠过期：像素出处未知，manifest 仍声称由本 plan 产出——标注无法
  变真，建议升 B）；**721 呈裁**（final_size 锁来源二义：期刊 profile 锁=C 保留 / 调用方锁=
  OB-DEVIATION；须先把 constraint_locks 拆出 locked_by 字段）。

### rendering.py（100 行：删 21 / 降格 79）
- **逐字自证闸 12 处删**（190/194/198/327/331/885/1727/1780/1789/1801/1837/1871）：取值域常常
  只有一个合法值，框架不验真伪；**已验证 rendering.py:292 把 "coordinate_system":"data" 硬编码
  写进 manifest——闸验抄写、账写常量**。⚠️ 落刀前置见呈裁 §4。
- 任意阈值 6 处删（203/208/213/240/336/2743）；2257 删（与 1883 重复抄件）；1931/1953 删
  （「reference 是对角线」的定义常识）。
- **"omission is prohibited" 家族 28 处降格→OB-COMPLETENESS**：画出完整那部分+**图面标注
  「M/N 行未画」**恒优于拒画（拒画丢全部信息，标注只丢「读者不知道缺失」——而标注恰好补上它）。
- **语义正确性 27 处降格→OB-SEMANTIC/OB-RANGE**（S5 主判据区）：矩阵对称/生存非增/ROC 非降/
  概率 [0,1] 等检查确实能发现异常——照画+**违例位置在图上标出**（如生存曲线上升段红标）+
  进 validation 报告+义务；发表态 referee 终审。
- 契约元数据缺失 13 处降格→OB-CONTRACT-META（`<unit undeclared>` 占位照画+义务派给上游）。
- 可安全自动补偿 11 处降格→OB-CAPACITY/OB-DEFAULTED-SELECTION（未知词表→中性字形+记账；
  超 max_labels→画前 k 个+标「另 m 个未标」；缺 frame_index→帧 0+图面标注）。

### relational_grammar.py（48：删 19 / 降格 29）
- **71 的 21 个调用点全删**：逐条确认**无一个被下游读取用于渲染决策**（allow_self_loops 无论
  值为何都照拒；isolated_nodes 数据结构上不可能；coordinate_system 已有真检查；provenance
  被 relational_rendering.py:183/408/621 硬编码写进 manifest）。⚠️ 前置同呈裁 §4。
- 17 处任意阈值/词表/仪式删（≤40 节点、≤6 分组、≤80 字符、kind 词表、「不守恒必须写
  rationale」等）。
- **586 删（科学错误）**：单子内部节点（ladder/单支谱系）在真实系统发育与层次聚类输出中合法
  存在——框架用美学假设否定真实拓扑。
- **166/379/689/812 降格（科学错误纠正）**：负权相关网络、回收/反馈流、centroid linkage 的
  inversion、ML 零/负枝长都是**真实科学对象**——硬拒即禁止一整类图；照画+标记号。
- **390 条件降格**（ribbon 厚度=数值编码，「画出来会自我谎称」的典范）：框架按 value×scale
  重画厚度+记补偿；**不实现补偿则升 B**（呈裁 §3）。
- 其余降格→OB-SEMANTIC/OB-CONTRACT-META/OB-COMPLETENESS（守恒失衡标在图上、树断开画最大
  连通分量+标「N 组件本图 1/N」）。

### 其余文件（要点）
- observation_rendering：85/393/446/496 删（阈值+抄固定字面量）；221 降格（开火 5 次全合规
  重算——转义务零损失）；其余降格。
- spatial：240/715/737/961 删（阈值）；466/559/598 降格（默认帧 0/块 0+图面「未指定，共 N」）；
  1300 降格（pure 不可得自动降 hybrid+记原因——「不许降级」是预测失败近亲）。
- planning：**725 删**（generative_allowed 事前授权闸；证据边界由 734 的 evidence_bearing=false
  B 墙独立守住）；738 删（与 imagegen_http:134 重复抄件）；580/635→OB-PUBSPEC；其余→
  OB-CONTRACT-META（372 可自动拆小多图）。
- geospatial：247 删（destagger 纯自证）；266 降格（**自动加 stride 并记补偿量**，不再要求
  人来加）；其余降格。
- trajectory：106 删（「连 all 也要显式写」）；139 删（阈值）；52/58/96 降格。
- **reviewer：249/269/318/335 删**（观察禁带判决字段→改剥离+记账；正则判词会误伤真实缺陷
  描述且 310 已有静默丢弃路径；「全干净必须空数组」=部分成功判全废）；233 降格（截断到 8+
  记「已截断 N 条」）。角色分离治理项见呈裁 §5。
- imagegen_http:130/134 删；reviewer_benchmark:49 删、63 改判 C（基准定义本身）、
  **91 升 B**（基准字体=度量刻度，换字体=基准分数变假，与 mcp_adapters:241 同型）；
  label_layout:44 删、**167 降格**（「找不到完美位置→拒绝并 remove 已放标注」极端形态；
  照放引线/半透明+audit 进 validation）；spatial_vector_export:178/186 降格（自动降 hybrid+
  记账）；composite/schematic_layout/electronic_structure 降格；
  **mcp_adapters:155/166 降格**（后端自证确定性/冒烟——与 qinp「ModelRoleUnavailable→
  fail closed」同构：后端可用+manifest 记复现风险）。
- 维持 B（复核确认）：mcp_adapters:241（科学值被改=图必然自我谎称且标注无法补救——本域唯一
  此类型）、planning:734、imagegen_http:132。

## 收敛地图（12 个 collector）
OB-COMPLETENESS(~40) ｜ OB-SEMANTIC(~45) ｜ OB-CONTRACT-META(~40) ｜ OB-RANGE(~10) ｜
OB-DEFAULTED-SELECTION(~12) ｜ OB-CAPACITY(~12) ｜ OB-DEVIATION(7) ｜ OB-LINEAGE(7) ｜
OB-PUBSPEC(6) ｜ OB-BACKEND-FIDELITY(5) ｜ OB-LABEL-PLACEMENT(~12) ｜ OB-VALIDATION-OPEN(3)。
前三组吃掉 125 条，全走同一套「照画+检查进 validation 报告+未过项成义务+该标注的标在图上」。

## 呈裁清单
1. **lifecycle:217**（外部改写的 hash 不符→建议升 B：像素出处未知，标注无法变真）。
2. **lifecycle:721**（锁来源二义：先拆 locked_by:"publication_profile"|"caller" 再分判）。
3. **relational_grammar:390 条件项**：不实现厚度补偿则升 B。
4. **删逐字断言闸的阻塞前置**：relational_rendering.py:183/408/621 与 rendering.py:292 把
   "upstream_computed"/"data" **硬编码写进 manifest**——只删闸不改账，产物变成框架无依据的
   出处断言（边界复核判否）。落刀必须同批改为记「上游声明值/not_declared」+
   provenance_verified:false。**此项做完删除才通过边界复核。**
5. **reviewer 全组删后的角色分离**：接受提示词约束+下游剥离越权字段（推荐），还是保留一条
   机械剥离（非拒绝）——wangd 拍板。

## 统计
删 73（29.2%）/ 降格 171（68.4%）/ 保留升格 4 / 呈裁 2（+3 条件/前置）。
最先执行的三刀：lifecycle:1401（fire_data 已取证）、lifecycle:2543（自带 None 分支）、
relational_grammar:71 一批（21 调用点四套语法，前置=呈裁 §4 manifest 记账修正）。
