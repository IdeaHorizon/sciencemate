# 判决书：core/（49 条；删 12 / 降格 16 / 保留升格 15 / 呈裁 6）

裁决人回读了全部 49 条 ±30 行真实上下文（含消费端）。三条改变战局的实测发现：

1. **`obligations.terminal_block()` 是死代码**——全仓无生产调用方；真正有牙齿的终态闸在
   `chat.py:1224`（`open_blocking_obligations` → 把 complete 改回 continue），而 `blocked`
   在 chat.py:1247 被做成停靠不终态 → **挂着 blocking 义务时 continuous 模式没有任何合法
   出口**。这是 qinp 事故的批量复制器（见呈裁 ④）。
2. **closure/executor 的 10 条「降级 incomplete」不是墙**：零拒绝分支，是 S2 如实记账本身，
   是拆除工作的接收地基，从裁决对象摘出（判保留）。
3. **`prereg_commitments.py:835` 已是档二形态**：estimated + basis + degraded_reason 公开
   申报即放行 = S4 declared deviation 的完整样板，全仓应抄。

## 判决表

| file:line | 判决 | 义务组 | 理由 |
|---|---|---|---|
| dispatch_gate.py:202 | 保留·升 A | — | 逐字节指纹熔断（S6），三条解除路径+env 阀在 |
| dispatch_gate.py:210 | 保留（随 202） | — | 指路文案的单一真相源 |
| closure.py:123/231/448 | 保留 | — | 事实谓词零拒绝分支（S2/S1） |
| closure.py:465 | 降格 | 收尾记账口径组 | 项目级长账不该降级本 run status（S2 失真） |
| closure.py:490 | 保留 | — | blocking 义务进 open_items 是 S1 兜底 |
| executor.py:1337/1358/1390 | 保留 | — | 如实标注非判决（S2） |
| obligations.py:882 | **删** | — | 死代码零调用方 |
| obligations.py:72/113/334/543 | 降格 | 终态清偿义务组 | 检查真、blocking 焊死假（S3；补救不在力内→死路） |
| obligations.py:148 | 保留 | — | blocking=False 只渲染——**档二目标形态样板** |
| obligations.py:742 | 降格 | 预注册兑现与偏离申报组 | 它要的本就是申报（S4），错在 blocking 让申报变牢笼 |
| verdict_authority.py:137 | 降格 | 裁决权归属组 | 环境事实 fail-closed 成死路；如实标 unverified+义务（S2） |
| verdict_authority.py:154 | 保留·升 B | — | 不指名 id 的裁决事后不可核（S1），零成本在力内 |
| verdict_authority.py:161/170 | **呈裁** | — | 裁决权分离=治理设计（档三点名） |
| prereg_commitments.py:816 | **删**（并入 154） | — | 重复抄件；合并条件=154 触发放宽为「有 research_state 或有冻结 prereg」 |
| prereg_commitments.py:825 | 降格 | 预注册兑现与偏离申报组 | 把可申报偏离做成不可能（S4） |
| prereg_commitments.py:835 | 保留·升 B | — | 未兑现翻 validated=机器可读 status 变假（S1）；**让步通道已在**，已是档二形态 |
| prereg_commitments.py:541 | 降格 | 预注册兑现与偏离申报组 | 拒冻会连锁锁死全项目；缺陷记在产物上+关闭时不判 validated 即可 |
| kb_promotion.py:487/175/231 | **呈裁** | — | org 晋升三查=治理项；实测 promote() 生产零调用方，活路径是 propose→人批 |
| domain_registry.py:573 | **呈裁** | — | 人批背书治理项；建议 provisional_leaf+人批义务 |
| tool_registry.py:215 | 保留·升 B | — | 同名覆盖=换掉所有节点的证人（S1）；豁免登记=已有让步形态 |
| session_driver.py:236 | 保留·升 A | — | 并发抢 pause 不可逆损坏（S6） |
| tasks.py:154 | **删** | — | 空 title 仪式；落盘记「(未命名)」 |
| tasks.py:160/183/223 | 降格 | 账本状态机如实迁移组 | 反常迁移如实留痕 from→to+理由，优于禁止（S2/S3） |
| tasks.py:185 | **删** | — | unblock 再 start 纯仪式；隐式解锁留痕 |
| tasks.py:193 | **删** | — | ≤1 in_progress 任意阈值 |
| tasks.py:221 | **删** | — | 字数闸；shared/tasks.py:141 同一抄件 |
| state.py:1551 | 降格 | 账本状态机如实迁移组 | refuted 终态=新证据永不能重开假说，违 S3；转换进 revision_history 账真 |
| state.py:725 | **删** | — | 空值仪式，零命中 |
| state.py:1555 | **删** | — | 字数闸+重复抄件（kb_schema:443 已有更强版） |
| memory.py:327/527 | **删** | — | 字数闸 ×2（同一常量两处抄件） |
| memory.py:533/539 | 降格 | 记忆送达与出处组 | 送达地址/出处检索价值真；拒入册让教训消失（S2）；539 框架只验非空不验真伪 |
| memory_forget.py:191 | **删** | — | 字数闸 |
| memory_forget.py:214 | 保留 | — | 误分：操作无对象的如实报错（C） |
| whiteboard.py:98 | 降格 | 工作区留痕组 | 防丢失价值→改为接受+旧板全文落 transcript，拒绝死 |
| org_canon.py:168 | **删** | — | 空值仪式+零命中；版本化产物可逆 |

## 收敛地图（7 个义务组）

| 组名 | 条目 | 检查什么 |
|---|---|---|
| 终态清偿义务组 | obligations 72/113/334/543 | 项目欠账在收尾时必须被应答：补齐/让步（附理由，永久进账本+局限节）/呈报；应答即清偿，不再焊死 complete |
| 预注册兑现与偏离申报组 | obligations 742、prereg 825/541 | 冻结 prereg 声明的设计/闭合条件/范围外新问题有无公开申报的兑现或偏离记录（样板=prereg:835） |
| 裁决权归属组 | verdict_authority 137（+161/170 呈裁） | 翻 validated/refuted 有无 Analysis 背书；核不了如实标 unverified+义务 |
| 账本状态机如实迁移组 | tasks 160/183/223、state 1551 | 反常状态迁移一律接受并如实留痕，见证不禁令 |
| 记忆送达与出处组 | memory 533/539 | 教训条目的送达地址与出处；缺则标 undelivered+补址义务 |
| 工作区留痕组 | whiteboard 98 | 覆盖写入前旧内容留证；内容取舍归 agent |
| 收尾记账口径组 | closure 465 | 项目长账进 summary open_items 但不降级单 run status |

## 呈裁清单

1. **verdict_authority 161/170（取证者不裁决自己的取证）**：建议=接受翻转但降级落盘
   `provisional + claimed_by=experiment` + Analysis 背书义务——保 S1 又消灭死路。须 wangd 拍板。
2. **kb_promotion 175/231/487（org 晋升三查）**：建议=三查降格为候选清单红字，promote()
   的硬拒保留为人批呈报材料。须 owner 拍板。
3. **domain_registry:573**：建议=允许注册标 provisional_leaf+人批义务。须 owner 拍板。
4. **跨界总闸 `chat.py:1224`**（core 义务判决的生效前提）：建议=保留闸但接受申报式让步
   （义务转 conceded，永久进账本+收尾清单+局限节，status 仍如实），referee 终审。与批 0
   让步原语一起落地。

## 统计
删 12 / 降格 16 / 保留升格 15（升 A 3、升 B 4）/ 呈裁 6。
