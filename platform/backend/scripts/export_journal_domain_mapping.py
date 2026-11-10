#!/usr/bin/env python3
"""导出可核对的全量二级学科—期刊关系；从后端环境运行。"""
from __future__ import annotations
import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'platform/backend'))
from app.services.literature_harvester import _journal_targets, _normal_journal


def export(*, scope_active: bool = False) -> Path:
    rows = list(csv.DictReader((ROOT / 'docs/xiaohongshu_journal_map.tsv').open(encoding='utf-8'), delimiter='\t'))
    catalog = json.loads((ROOT / 'platform/backend/app/data/discipline_catalog.json').read_text())
    rules = json.loads((ROOT / 'nodes/literature/tools/journal_domain_scope_rules.json').read_text())['rules']
    report = json.loads((ROOT / 'docs/journal_mapping_changes_20260928.json').read_text())
    by = defaultdict(list)
    for row in rows:
        by[row['二级学科代码']].append(row)
    labels = {c['domain']: c['label'] for g in catalog for c in g['categories']}
    assert set(by) == set(labels) and len(labels) == 394
    selected = {
        code: {_normal_journal(t['journal']) for t in _journal_targets({code}).values()}
        for code in labels
    }
    global_targets = _journal_targets()
    global_selected = defaultdict(set)
    for target in global_targets.values():
        for code in target['domains']:
            global_selected[code].add(_normal_journal(target['journal']))
    actions = Counter(c['action'] for c in report['changes'])
    lines = [
        '# 二级学科与期刊映射全表', '',
        '更新日期：2026-09-28。按平台当前学科目录排序，便于搜索学科名称或代码。', '',
        '## 覆盖与阅读说明', '',
        f'- {len(catalog)} 个一级学科分组、{len(labels)} 个二级学科，全部有映射。',
        f'- 修正前 {report["before_pairs"]:,} 条关系，修正后 {len(rows):,} 条关系；同一本期刊可以属于多个学科。',
        f'- 按当前采集器名称归一后约 {len({_normal_journal(r["journal_name"]) for r in rows}):,} 本期刊；别名及更名识别会影响这一统计。',
        '- **单科采集**：仅订阅该二级学科时，当前采集器会选择的期刊，每科最多 10 本。',
        '- **全科采集**：同时扫描全部二级学科时的分配，每本期刊最多关联 5 个二级学科。实际用户多选的组合可能不同。',
        '- **相关性**：核心优先、普通候选、需主题证据。它是本次修正的排序提示，不是分区排名。',
        '- **主题筛选**：较窄学科中的综合期刊，只对标题/摘要有主题证据的论文建立学科推荐；用户直接订阅期刊仍可查看该刊论文。',
        ('- 主题规则及相关性排序已接入采集、投影和推荐。' if scope_active else '- **实施状态：映射表已修正；主题规则与相关性排序代码尚待接入。下表“采集”列按当前代码实际计算。**'),
        '', '## 审计范围及限制', '',
        '本次逐项查看了全部 394 个二级学科的候选期刊，并校验目录代码、学科标签、ISSN 格式/校验位、重复关系及实际选择逻辑。明显错配结合期刊官网、出版机构资料核对后修正。', '',
        '**全量初筛不等于全部期刊已逐一核对官网范围。**跨学科期刊可能合理；本次只清除明确错配，其他关系保留供核对。部分较窄学科增加了论文主题规则，属于可复核的关键词筛选，不是完整的语义分类，可能漏掉标题和摘要未明示主题的论文。', '',
        '历史字段 `manual_source_verified / manually_verified` 不能单独证明学科归属可靠：历史补齐脚本曾把“LLM 候选 + ISSN/刊名检索”写成该状态。下表不把这个旧标签当作本次官网核验结论，也不展示尚未重新核验的历史 CAS/JCR 分区值。', '',
        '## 本次修正', '',
        f'- 删除 {actions["remove"]} 条不合理映射，补入 {actions["add"]} 条民族艺术检索入口，合并 {actions["deduplicate"]} 条重复关系。',
        f'- 统一 {actions["label"]} 条记录中的学科标签或一级代码；修正 {actions["rename"]} 条现刊名称。',
        f'- 为 {len(rules)} 个二级学科建立论文主题规则；汉语专刊设置核心优先。',
        '- 语言：移除 Mongolian Journal of Strategic Studies → 中国少数民族语言文学。',
        '- 社科/法学：移除性别研究刊 → 人口学、生物学刊 → 环境与资源保护法学。',
        '- 医学：移除航空工程刊 → 航空、航天与航海医学；纠正 NEJM 现刊名称和 ISSN。中医专科增加中医及对应专科证据。',
        '- 工程：水声、轮机、水工结构、水利水电、制革、制糖等采用主题规则，避免综合期刊整刊归入较窄学科。',
        '- 农学/食品：茶学只纳入茶相关论文；水产品加工须有水产原料及食品/加工/贮藏证据。',
        '- 理学：Nature、Science 中的生化/分子生物学归属须有论文主题证据。', '',
        '### 删除关系明细', '', '| 代码 | 二级学科 | 期刊 | 原因 |', '|---|---|---|---|',
    ]
    def esc(value):
        return str(value or '—').replace('|', '\\|').replace('\n', ' ')
    for change in report['changes']:
        if change['action'] == 'remove':
            lines.append(f'| {change["code"]} | {labels[change["code"]]} | {esc(change["journal"])} | {esc(change["reason"])} |')
    lines += ['', '### 期刊覆盖较少的学科', '',
              '下面 21 个学科只有 1–3 本入口，可能限制内容数量；不是零覆盖。本次没有为了凑满 10 本而补入缺少依据的期刊。', '',
              '| 代码 | 二级学科 | 映射期刊数 |', '|---|---|---:|']
    for code in labels:
        if len(by[code]) <= 3:
            lines.append(f'| {code} | {labels[code]} | {len(by[code])} |')
    lines += ['', '## 一级学科索引', '', '| 一级学科代码 | 一级学科 | 二级学科数 | 映射关系数 |', '|---|---|---:|---:|']
    for g in catalog:
        code = g['archive'].removeprefix('cas:')
        lines.append(f'| {code} | [{esc(g["label"])}](#g{code}) | {len(g["categories"])} | {sum(len(by[c["domain"]]) for c in g["categories"])} |')
    lines += ['', '## 完整映射', '']
    for g in catalog:
        group_code = g['archive'].removeprefix('cas:')
        lines += [f'<a id="g{group_code}"></a>', '', f'### {group_code} {g["label"]}', '']
        for c in g['categories']:
            code = c['domain']; items = by[code]
            lines += [f'<a id="d{code}"></a>', '', f'#### {code} {c["label"]}', '',
                      f'映射 {len(items)} 本；单科采集 {len(selected[code])} 本；全科采集 {len(global_selected[code])} 本。', '']
            if code in rules:
                rule = rules[code]
                lines += ['主题规则：' + rule['description'], '']
                if rule.get('direct_journals'):
                    lines += ['直接映射例外：' + '；'.join(rule['direct_journals']) + '。', '']
            lines += ['| 期刊 | ISSN | 相关性 | 单科采集 | 全科采集 |', '|---|---|---|---|---|']
            for row in items:
                journal = row['journal_name']; key = _normal_journal(journal)
                priority = {'0':'核心优先','1':'普通候选','2':'需主题证据'}.get(row.get('相关性优先级'), '普通候选')
                lines.append(f'| {esc(journal)} | {esc(row["issn"])} | {priority} | {"是" if key in selected[code] else "候选"} | {"是" if key in global_selected[code] else "候选"} |')
            lines += ['']
    lines += ['## 本次核对的主要资料', '',
              '- [Language：美国语言学会的期刊范围](https://www.lsadc.org/language)',
              '- [蒙古战略研究期刊：ISSN 出版机构记录](https://portal.issn.org/resource/ISSN/2312-9670)',
              '- [Biology Natural Resources Journal：官方生物学范围](https://journal.unirow.ac.id/index.php/binar)',
              '- [Sukupuolentutkimus–Genusforskning：出版协会范围说明](https://sukupuolentutkimus.fi/sukupuolentutkimus-lehti/kirjoita/)',
              '- [Cultural Diversity & Ethnic Minority Psychology：APA 期刊范围](https://www.apa.org/pubs/journals/cdp/)',
              '- [NEJM：官方期刊与 ISSN](https://www.nejm.org/about-nejm/editorial-policies)',
              '- [Ethnomusicology：出版商介绍及 ISSN](https://www.press.uillinois.edu/journals/?id=ethno)',
              '- [Asian Ethnology：官方研究范围](https://asianethnology.org/about)',
              '- [Asian Theatre Journal：夏威夷大学出版社的期刊范围与 ISSN](https://uhpress.hawaii.edu/title/atj/)', '',
              '## 数据与复核', '',
              '- 当前映射：`docs/xiaohongshu_journal_map.tsv`',
              '- 主题规则：`nodes/literature/tools/journal_domain_scope_rules.json`',
              '- 完整变更记录：`docs/journal_mapping_changes_20260928.json`',
              '- 学科目录：`platform/backend/app/data/discipline_catalog.json`',
              '- 本文导出器：`platform/backend/scripts/export_journal_domain_mapping.py`', '']
    dest = ROOT / 'docs/二级学科与期刊映射_20260928.md'
    dest.write_text('\n'.join(lines), encoding='utf-8')
    print(json.dumps({'document':str(dest),'disciplines':len(labels),'pairs':len(rows),'scope_active':scope_active},ensure_ascii=False))
    return dest


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--scope-active', action='store_true')
    args = parser.parse_args()
    export(scope_active=args.scope_active)
