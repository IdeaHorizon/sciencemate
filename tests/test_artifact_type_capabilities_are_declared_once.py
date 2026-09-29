"""artifact 类型的性质只声明一次，各机制查表推导。

## 现场

"这个 artifact 类型是什么性质"这个问题，曾经有三份互不知情的答案：

    core/prereg_commitments._RESULT_TYPES              谁能勾除闭合条目
    core/loop_hooks_builtin._CITATION_CHECK_TARGET_TYPES  谁要被查引用诚信
    nodes/writing/artifact_expectations.yaml           writing 的硬性上游

三份名单都是硬编码。后果是机械的：**新增一种证据类型时，漏改哪份，哪道闸就
对它默认失效**，而且失效方向全是放行 —— 不查引用、不算勾账。这正是"护栏要
扫盘不要写名单"那条教训的形状，只不过这次名单散在三个文件里。

对照组一直就在隔壁：`shared/tools/builtin._producing_output_owners()` 遍历
harness 推导所有权，新节点一声明就自动受保护，从不硬编码。

## 这组测试守什么

不是"当前有哪几种类型"（那会随业务变，钉死它只会制造无意义的红）。守的是
**推导关系本身**：机制必须从注册表读，注册表必须自洽。所以这里既验行为等价，
也验"名单没有偷偷长回来"。
"""
from __future__ import annotations

import inspect
import re

import pytest

from shared.lib import artifact_policy as ap


class TestRegistryIsTheSingleSource:
    def test_the_three_facets_are_orthogonal_not_aliases(self):
        """三个属性各自回答一个机制的问题，不是同一个属性的三个名字。

        若它们恒等，就该合并成一个；正因为不等，才必须分开声明。
        """
        evidence = set(ap.evidence_record_types())
        ledger = set(ap.discharge_ledger_types())
        cited = set(ap.citation_checked_types())
        assert evidence != ledger, "结果表能勾账但不是权威研究记录"
        assert evidence != cited, "manuscript 要查引用但不是证据记录"

    def test_every_evidence_record_can_also_discharge(self):
        """权威记录必然能勾账 —— 由 `_validate_registry` 在 import 时保证。

        反向不成立是有意的：`clean_results` 能勾账，但没有裁决理由和可信度段落，
        单凭它写论文等于拿一张数字表当研究记录。
        """
        assert set(ap.evidence_record_types()) <= set(ap.discharge_ledger_types())

    def test_a_mis_declared_type_fails_loudly_at_startup(self):
        """声明表是静态配置：配错了当场炸，不许以别的面目在运行时出现。

        没有这道校验，症状会是"闭合条目怎么勾不上"—— 离病因隔着好几层。
        """
        original = dict(ap.POLICY)
        try:
            ap.POLICY["bogus_record"] = {
                "retention": "transient", "evidence_record": True,
            }
            with pytest.raises(ValueError, match="bogus_record"):
                ap._validate_registry()
        finally:
            ap.POLICY.clear()
            ap.POLICY.update(original)

    def test_accessors_are_total_over_unknown_types(self):
        """没声明过的类型一律返回 False —— fail-closed。

        默认放行会让"忘了声明"变成"这类工件不受任何约束"，而那正是最该拦的
        情形（比如有人新造一种 log 类型就绕开了引用诚信闸）。
        """
        for probe in ("", "never_declared_type", "random"):
            assert ap.is_evidence_record(probe) is False
            assert ap.carries_discharge_ledger(probe) is False
            assert ap.cites_kb_claims(probe) is False


class TestMechanismsDeriveRatherThanEnumerate:
    def test_closure_ledger_reads_the_registry(self):
        """闭合账本按类型属性过滤，不按名单。"""
        from core import prereg_commitments

        source = inspect.getsource(prereg_commitments._scan_result_metadata)
        assert "carries_discharge_ledger" in source
        assert not re.search(r'==\s*["\']experiment_log', source)

    def test_citation_gate_reads_the_registry(self):
        """引用诚信闸同上。"""
        from core import loop_hooks_builtin

        source = inspect.getsource(loop_hooks_builtin._citation_check_target_types)
        assert "citation_checked_types" in source

    def test_no_module_level_type_name_lists_remain(self):
        """名单不许长回来 —— 判据必须是**扫盘**，不能是名单。

        这条原来自己就是名单写的：只扫 `prereg_commitments` / `loop_hooks_builtin`
        两个模块，正则里只认 `experiment_log|manuscript|clean_results` 三个名字。

        2026-08-19 它当场漏了下一张名单：`core/artifact_roles.FRAMEWORK_INTERNAL_TYPES`
        —— 第三个模块、成员是 compression_log / resource_profile / …，两条轴上同时
        不占。文本不冲突、测试全绿，于是 main 上一度有第四份类型性质声明。
        护栏要扫盘不要写名单，这条规矩也管护栏自己。

        现在：AST 遍历 core/ 与 shared/ 全部模块，凡模块级的字符串集合里出现
        **两个及以上**已注册 artifact 类型，就判定为类型名单。两个起判是为了不
        误伤恰好含一个同名字符串的普通常量。
        """
        import ast
        import pathlib

        #: 扫盘一上线就抓出的存量名单。**这是欠账清单，不是判据** —— 判据仍是
        #: 扫盘，新增的名单照样红。每条都得变成注册表里的一条性质，删一条少一条：
        #:
        #:   core/recall.py::RESEARCH_PRODUCT_ARTIFACT_TYPES
        #:       "算不算研究产物"（召回面）→ 该是一条性质
        #:   core/traceability.py::UPSTREAM_TYPES
        #:       "能不能做溯源上游" → 该是一条性质
        #:   core/quality_checks.py::_CITED_STOPWORDS
        #:       疑似误报：引用匹配的停用词表，恰好含类型名。核实后要么加豁免
        #:       理由，要么同样收进注册表。
        #:
        #: 2026-08-19 记：旧护栏只扫两个模块、只认三个类型名，这三张一张都没看见。
        KNOWN_DEBT = {
            "core/recall.py": {"RESEARCH_PRODUCT_ARTIFACT_TYPES"},
            "core/traceability.py": {"UPSTREAM_TYPES"},
            "core/quality_checks.py": {"_CITED_STOPWORDS"},
        }
        known = set(ap.POLICY)
        registry_file = pathlib.Path(ap.__file__).resolve()
        root = pathlib.Path(__file__).resolve().parent.parent
        offenders: list[str] = []

        for path in list((root / "core").rglob("*.py")) + list((root / "shared").rglob("*.py")):
            if path.resolve() == registry_file:
                continue                      # 注册表自己就是那份声明
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
            except SyntaxError:
                continue
            for node in tree.body:            # 只看模块级
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                value = node.value
                if not isinstance(value, (ast.List, ast.Tuple, ast.Set)):
                    if not (isinstance(value, ast.Call)
                            and isinstance(value.func, ast.Name)
                            and value.func.id in {"frozenset", "set", "tuple", "list"}
                            and value.args
                            and isinstance(value.args[0], (ast.List, ast.Tuple, ast.Set))):
                        continue
                    value = value.args[0]
                members = {e.value for e in value.elts
                           if isinstance(e, ast.Constant) and isinstance(e.value, str)}
                hits = members & known
                if len(hits) >= 2:
                    names = [t.id for t in ([node.target] if isinstance(node, ast.AnnAssign)
                                            else node.targets) if isinstance(t, ast.Name)]
                    rel = str(path.relative_to(root))
                    if names and set(names) & KNOWN_DEBT.get(rel, set()):
                        continue          # 存量欠账，见上面清单
                    offenders.append(f"{rel}::{names or '?'} → {sorted(hits)}")

        assert not offenders, (
            "又出现了注册表外的 artifact 类型名单：\n  "
            + "\n  ".join(offenders)
            + "\n把这条性质声明进 shared/lib/artifact_policy 的 POLICY，各机制查表。"
        )

    def test_the_debt_list_only_shrinks(self):
        """欠账清单不许变长 —— 它是待还的债，不是可以随手加名字的豁免口。"""
        import inspect as _i

        src = _i.getsource(self.test_no_module_level_type_name_lists_remain)
        assert src.count('": {"') == 3, (
            "KNOWN_DEBT 条目数变了。删条目（还债）随时欢迎；加条目意味着又长了一张"
            "注册表外的类型名单 —— 那正是这组测试要挡的事。"
        )

    def test_framework_written_artifacts_are_all_classified(self):
        """框架代码自己落盘的每一种类型都必须在注册表里有出处。

        与上一条互补：上一条挡"名单长回来"，这一条挡"新的自动产物没人归类" ——
        后者的代价实测过：compression_log 没被归类 → 被列进 curator 整合目标 →
        `scan_artifact_disagreements` 扫不到它 → 门禁 fail-closed → 无声死锁。
        """
        import pathlib
        import re as _re

        root = pathlib.Path(__file__).resolve().parent.parent
        found: set[str] = set()
        for path in (root / "core").rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            found |= set(_re.findall(r"""artifact_type\s*=\s*["']([a-z_]+)["']""", text))

        from shared.tools.builtin import _producing_output_owners

        declared = set(_producing_output_owners())
        unclassified = {t for t in found if t not in ap.POLICY and t not in declared}
        assert not unclassified, (
            f"框架代码落盘了这些 artifact 类型，但注册表里没有它们：{sorted(unclassified)}。"
            "是框架内务就加 framework_internal: True；是节点交付物就去 harness 里声明。"
        )


class TestFrameworkInternalIsAProperty:
    """「是不是框架内务」是一条类型性质，和别的性质住同一个地方。"""

    def test_internal_types_are_excluded_from_integration(self):
        ids = ["clean_results__UK", "compression_log__t21", "experiment_log__Run"]
        assert ap.integration_targets(ids) == ["clean_results__UK", "experiment_log__Run"]

    def test_optional_node_artifacts_are_still_targets(self):
        """合法产物 ≠ 必需产出 —— 用"声明过才算"当判据会把可选科学产物漏出整合。"""
        assert ap.is_integration_target("hypothesis_cluster_report__Clustering")

    def test_ranking_and_integration_share_one_criterion(self):
        ids = ["compression_log__a", "pre_registration__Q1", "research_state__v4"]
        ranked = ap.rank_for_downstream(ids)
        assert set(ranked[:2]) == set(ap.integration_targets(ids))

    def test_internal_is_mutually_exclusive_with_the_evidence_facets(self):
        for t in ap.framework_internal_types():
            assert not ap.is_evidence_record(t)
            assert not ap.carries_discharge_ledger(t)
            assert not ap.cites_kb_claims(t)


class TestMembershipIsDeliberate:
    """三个集合的成员逐字钉死 —— 金丝雀，不是清单。

    钉死不是为了记录"现在有哪几种"（那会随业务变），是为了让**任何新增都必须
    过一次人眼**：往注册表里加一个 `evidence_record: True`，就等于给这个类型
    开通了勾除闭合条目、被 writing 引用、进证据链的权限。那是科学可信度的地基，
    不该被顺手加进去。

    改这几行是**正常的**（observation_log 就是这么加进来的），但改动必须是
    有意的，而且 diff 会明确显示新增了什么权限。
    """

    def test_evidence_records_are_the_three_evidence_producing_modalities(self):
        """权威研究执行记录 = 取证的三种模态，一种一份记录。

        干预式（experiment：让世界产生新数据）、检视式（observation：系统性地
        看世界已留下的记录）、演绎式（derivation：从已承诺的前提推出新命题）。
        2026-08-22 加入第三支 —— 上一版这条测试的 docstring 就写着"将来若加入
        会在这里显形"，它显形了。

        判据是**模态**，不是学科：跑一个中世纪贸易的 ABM 模拟是 experiment
        （哪怕课题是历史学），流行病学队列研究是 observation（哪怕它是自然
        科学），推导一个统计量的渐近分布是 derivation（哪怕它服务于一个生物
        实验）。学科型的新增（"化学节点"）过不了这道门；新模态才过得了。
        """
        assert ap.evidence_record_types() == (
            "derivation_log", "experiment_log", "observation_log",
        )

    def test_discharge_ledger_adds_supporting_result_tables(self):
        """能勾账的比权威记录多一个 `clean_results`：它是支撑数据表。

        它能携带兑现数据，但没有裁决理由和可信度段落 —— 所以不是权威记录，
        writing 不认它做上游。
        """
        assert ap.discharge_ledger_types() == (
            "clean_results", "derivation_log", "experiment_log", "observation_log",
        )

    def test_citation_gate_covers_prose_that_cites_claims(self):
        """引用诚信闸覆盖所有会引用 KB claim 的正文：三份证据记录 + 稿件。

        derivation_log 在内：一条 `cited_theorem` justification 会引 KB claim
        id，而**引一条查不到出处的定理，等于把假设伪装成已知** ——
        幻觉引用在演绎链里比在散文里更危险，它会被后续每一步继承。
        """
        assert set(ap.citation_checked_types()) == {
            "derivation_log", "experiment_log", "observation_log", "manuscript",
        }

    def test_every_evidence_record_type_has_exactly_one_producing_owner(self):
        """证据类型必须有主 —— 无主 = 谁都能凭空造证据。

        所有权由扫盘推导（`_producing_output_owners` 遍历各节点声明的
        required_output），所以这条测试同时验了两件事：类型声明了，且**真有
        一个节点在产它**。只在注册表里声明、没有节点产出的证据类型，是一个
        任何人都能 save_artifact 填进去的空位。
        """
        from shared.tools.builtin import _producing_output_owners

        owners = _producing_output_owners()
        for artifact_type in ap.evidence_record_types():
            assert len(owners.get(artifact_type) or set()) == 1, (
                f"{artifact_type} 没有唯一的 producing owner：{owners.get(artifact_type)}"
            )
