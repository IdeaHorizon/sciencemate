"""域注册表：知识的送达地址簿 —— 投稿选分类，不是自由填空。

## 为什么是注册表，不是自由文本

`domain` 是正典层的寻址键（综述按域组织、开题按域命中）。自由文本的域会
碎片化：两次晋升写出「MLIP」和「机器学习势」两个域 → 同一领域两篇综述
各自演化 = 一个问题两个真相源。词表必须受控。

## 为什么骨架抄 arXiv

别名归并问题世界已经替我们解完了：arXiv 分类树标准、稳定、机器可读，
且书目条目本来就带着它。这和投期刊选分类是同一个动作 —— 晋升就是投稿时刻。

## 结构

    骨架（arXiv 分类，内置只读）        cond-mat.stat-mech / physics.comp-ph / cs.LG …
      └ org 本地叶（受控增长）          physics.comp-ph/mlip-robustness

- 挂叶必须声明骨架父节点，不许悬空
- 新叶不单开审批流：**随晋升人批批次一起注册**（promote 的 approved_by 顺带背书）
- 一条 entry：主域 1 个（正典吸收按它走，保单一真相源）+ 交叉域 ≤ 2（检索/送达命中用）
- 粒度：卡片只按主域粒度写一遍；粗粒度转述是上层正典综述的活，不让 entry 多说法

## 契约

拒收必须列出最近匹配 + 挂新叶的语法 —— 合法取值只在运行时报错 = 逼模型猜
（27 次 draft 的教训）。

见 docs/RFC_KB_TWO_TIERS_20260820.md §8 + docs/KB_SYSTEM_STATE_20260821.md §六.2。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

#: 一条 entry 至多几个交叉域（主域另算）。
MAX_CROSS_DOMAINS = 2

#: 拒收时给出的最近匹配条数。
SUGGEST_K = 6

#: arXiv 分类骨架（2024 版官方分类）。archive → subject 后缀元组；
#: 空元组 = archive 本身就是叶（gr-qc / hep-th 这类单层分类）。
#: 内置只读 —— 骨架不随 org 演化，演化的只有本地叶。
ARXIV_SPINE: dict[str, tuple[str, ...]] = {
    "astro-ph": ("CO", "EP", "GA", "HE", "IM", "SR"),
    "cond-mat": ("dis-nn", "mes-hall", "mtrl-sci", "other", "quant-gas",
                 "soft", "stat-mech", "str-el", "supr-con"),
    "gr-qc": (),
    "hep-ex": (),
    "hep-lat": (),
    "hep-ph": (),
    "hep-th": (),
    "math-ph": (),
    "nlin": ("AO", "CD", "CG", "PS", "SI"),
    "nucl-ex": (),
    "nucl-th": (),
    "physics": ("acc-ph", "ao-ph", "app-ph", "atm-clus", "atom-ph", "bio-ph",
                "chem-ph", "class-ph", "comp-ph", "data-an", "ed-ph",
                "flu-dyn", "gen-ph", "geo-ph", "hist-ph", "ins-det", "med-ph",
                "optics", "plasm-ph", "pop-ph", "soc-ph", "space-ph"),
    "quant-ph": (),
    "math": ("AC", "AG", "AP", "AT", "CA", "CO", "CT", "CV", "DG", "DS", "FA",
             "GM", "GN", "GR", "GT", "HO", "IT", "KT", "LO", "MG", "MP", "NA",
             "NT", "OA", "OC", "PR", "QA", "RA", "RT", "SG", "SP", "ST"),
    "cs": ("AI", "AR", "CC", "CE", "CG", "CL", "CR", "CV", "CY", "DB", "DC",
           "DL", "DM", "DS", "ET", "FL", "GL", "GR", "GT", "HC", "IR", "IT",
           "LG", "LO", "MA", "MM", "MS", "NA", "NE", "NI", "OH", "OS", "PF",
           "PL", "RO", "SC", "SD", "SE", "SI", "SY"),
    "q-bio": ("BM", "CB", "GN", "MN", "NC", "OT", "PE", "QM", "SC", "TO"),
    "q-fin": ("CP", "EC", "GN", "MF", "PM", "PR", "RM", "ST", "TR"),
    "stat": ("AP", "CO", "ME", "ML", "OT", "TH"),
    "eess": ("AS", "IV", "SP", "SY"),
    "econ": ("EM", "GN", "TH"),
}

#: archive 的人读名（arXiv 官方分类名）。
#:
#: 词表本身是给机器寻址用的（`cond-mat.mtrl-sci`），但凡是要让人**挑**域的
#: 地方 —— 资讯流的兴趣选择、晋升时的域下拉 —— 都得有人读得懂的名字。名字
#: 与 slug 必须同处一个文件：分开放就是两份会各自演化的词表，而分叉时没有
#: 任何一层会报错（新增分类照样能寻址，只是在 UI 上显示成一个 slug）。
#: `tests/test_domain_registry.py` 机械核对二者逐项对齐。
ARCHIVE_LABELS: dict[str, str] = {
    "astro-ph": "Astrophysics",
    "cond-mat": "Condensed Matter",
    "gr-qc": "General Relativity and Quantum Cosmology",
    "hep-ex": "High Energy Physics — Experiment",
    "hep-lat": "High Energy Physics — Lattice",
    "hep-ph": "High Energy Physics — Phenomenology",
    "hep-th": "High Energy Physics — Theory",
    "math-ph": "Mathematical Physics",
    "nlin": "Nonlinear Sciences",
    "nucl-ex": "Nuclear Experiment",
    "nucl-th": "Nuclear Theory",
    "physics": "Physics",
    "quant-ph": "Quantum Physics",
    "math": "Mathematics",
    "cs": "Computer Science",
    "q-bio": "Quantitative Biology",
    "q-fin": "Quantitative Finance",
    "stat": "Statistics",
    "eess": "Electrical Engineering and Systems Science",
    "econ": "Economics",
}

#: subject 的人读名，键是**全 slug**（不是后缀）—— `cs.CG` 和 `math.CG` 是
#: 两回事，按后缀存会让它们互相覆盖。
SUBJECT_LABELS: dict[str, str] = {
    "astro-ph.CO": "Cosmology and Nongalactic Astrophysics",
    "astro-ph.EP": "Earth and Planetary Astrophysics",
    "astro-ph.GA": "Astrophysics of Galaxies",
    "astro-ph.HE": "High Energy Astrophysical Phenomena",
    "astro-ph.IM": "Instrumentation and Methods for Astrophysics",
    "astro-ph.SR": "Solar and Stellar Astrophysics",
    "cond-mat.dis-nn": "Disordered Systems and Neural Networks",
    "cond-mat.mes-hall": "Mesoscale and Nanoscale Physics",
    "cond-mat.mtrl-sci": "Materials Science",
    "cond-mat.other": "Other Condensed Matter",
    "cond-mat.quant-gas": "Quantum Gases",
    "cond-mat.soft": "Soft Condensed Matter",
    "cond-mat.stat-mech": "Statistical Mechanics",
    "cond-mat.str-el": "Strongly Correlated Electrons",
    "cond-mat.supr-con": "Superconductivity",
    "nlin.AO": "Adaptation and Self-Organizing Systems",
    "nlin.CD": "Chaotic Dynamics",
    "nlin.CG": "Cellular Automata and Lattice Gases",
    "nlin.PS": "Pattern Formation and Solitons",
    "nlin.SI": "Exactly Solvable and Integrable Systems",
    "physics.acc-ph": "Accelerator Physics",
    "physics.ao-ph": "Atmospheric and Oceanic Physics",
    "physics.app-ph": "Applied Physics",
    "physics.atm-clus": "Atomic and Molecular Clusters",
    "physics.atom-ph": "Atomic Physics",
    "physics.bio-ph": "Biological Physics",
    "physics.chem-ph": "Chemical Physics",
    "physics.class-ph": "Classical Physics",
    "physics.comp-ph": "Computational Physics",
    "physics.data-an": "Data Analysis, Statistics and Probability",
    "physics.ed-ph": "Physics Education",
    "physics.flu-dyn": "Fluid Dynamics",
    "physics.gen-ph": "General Physics",
    "physics.geo-ph": "Geophysics",
    "physics.hist-ph": "History and Philosophy of Physics",
    "physics.ins-det": "Instrumentation and Detectors",
    "physics.med-ph": "Medical Physics",
    "physics.optics": "Optics",
    "physics.plasm-ph": "Plasma Physics",
    "physics.pop-ph": "Popular Physics",
    "physics.soc-ph": "Physics and Society",
    "physics.space-ph": "Space Physics",
    "math.AC": "Commutative Algebra",
    "math.AG": "Algebraic Geometry",
    "math.AP": "Analysis of PDEs",
    "math.AT": "Algebraic Topology",
    "math.CA": "Classical Analysis and ODEs",
    "math.CO": "Combinatorics",
    "math.CT": "Category Theory",
    "math.CV": "Complex Variables",
    "math.DG": "Differential Geometry",
    "math.DS": "Dynamical Systems",
    "math.FA": "Functional Analysis",
    "math.GM": "General Mathematics",
    "math.GN": "General Topology",
    "math.GR": "Group Theory",
    "math.GT": "Geometric Topology",
    "math.HO": "History and Overview",
    "math.IT": "Information Theory",
    "math.KT": "K-Theory and Homology",
    "math.LO": "Logic",
    "math.MG": "Metric Geometry",
    "math.MP": "Mathematical Physics",
    "math.NA": "Numerical Analysis",
    "math.NT": "Number Theory",
    "math.OA": "Operator Algebras",
    "math.OC": "Optimization and Control",
    "math.PR": "Probability",
    "math.QA": "Quantum Algebra",
    "math.RA": "Rings and Algebras",
    "math.RT": "Representation Theory",
    "math.SG": "Symplectic Geometry",
    "math.SP": "Spectral Theory",
    "math.ST": "Statistics Theory",
    "cs.AI": "Artificial Intelligence",
    "cs.AR": "Hardware Architecture",
    "cs.CC": "Computational Complexity",
    "cs.CE": "Computational Engineering, Finance, and Science",
    "cs.CG": "Computational Geometry",
    "cs.CL": "Computation and Language",
    "cs.CR": "Cryptography and Security",
    "cs.CV": "Computer Vision and Pattern Recognition",
    "cs.CY": "Computers and Society",
    "cs.DB": "Databases",
    "cs.DC": "Distributed, Parallel, and Cluster Computing",
    "cs.DL": "Digital Libraries",
    "cs.DM": "Discrete Mathematics",
    "cs.DS": "Data Structures and Algorithms",
    "cs.ET": "Emerging Technologies",
    "cs.FL": "Formal Languages and Automata Theory",
    "cs.GL": "General Literature",
    "cs.GR": "Graphics",
    "cs.GT": "Computer Science and Game Theory",
    "cs.HC": "Human-Computer Interaction",
    "cs.IR": "Information Retrieval",
    "cs.IT": "Information Theory",
    "cs.LG": "Machine Learning",
    "cs.LO": "Logic in Computer Science",
    "cs.MA": "Multiagent Systems",
    "cs.MM": "Multimedia",
    "cs.MS": "Mathematical Software",
    "cs.NA": "Numerical Analysis",
    "cs.NE": "Neural and Evolutionary Computing",
    "cs.NI": "Networking and Internet Architecture",
    "cs.OH": "Other Computer Science",
    "cs.OS": "Operating Systems",
    "cs.PF": "Performance",
    "cs.PL": "Programming Languages",
    "cs.RO": "Robotics",
    "cs.SC": "Symbolic Computation",
    "cs.SD": "Sound",
    "cs.SE": "Software Engineering",
    "cs.SI": "Social and Information Networks",
    "cs.SY": "Systems and Control",
    "q-bio.BM": "Biomolecules",
    "q-bio.CB": "Cell Behavior",
    "q-bio.GN": "Genomics",
    "q-bio.MN": "Molecular Networks",
    "q-bio.NC": "Neurons and Cognition",
    "q-bio.OT": "Other Quantitative Biology",
    "q-bio.PE": "Populations and Evolution",
    "q-bio.QM": "Quantitative Methods",
    "q-bio.SC": "Subcellular Processes",
    "q-bio.TO": "Tissues and Organs",
    "q-fin.CP": "Computational Finance",
    "q-fin.EC": "Economics",
    "q-fin.GN": "General Finance",
    "q-fin.MF": "Mathematical Finance",
    "q-fin.PM": "Portfolio Management",
    "q-fin.PR": "Pricing of Securities",
    "q-fin.RM": "Risk Management",
    "q-fin.ST": "Statistical Finance",
    "q-fin.TR": "Trading and Market Microstructure",
    "stat.AP": "Applications",
    "stat.CO": "Computation",
    "stat.ME": "Methodology",
    "stat.ML": "Machine Learning",
    "stat.OT": "Other Statistics",
    "stat.TH": "Statistics Theory",
    "eess.AS": "Audio and Speech Processing",
    "eess.IV": "Image and Video Processing",
    "eess.SP": "Signal Processing",
    "eess.SY": "Systems and Control",
    "econ.EM": "Econometrics",
    "econ.GN": "General Economics",
    "econ.TH": "Theoretical Economics",
}

#: archive 的中文名。与英文名**同处一个文件**，理由同上：分开放就是两份会
#: 各自演化的词表，而分叉时不报错（只是界面上中英混排）。
#: `tests/test_domain_registry.py` 机械核对中英两套都逐项覆盖骨架。
ARCHIVE_LABELS_ZH: dict[str, str] = {
    "astro-ph": "天体物理",
    "cond-mat": "凝聚态物理",
    "gr-qc": "广义相对论与量子宇宙学",
    "hep-ex": "高能物理·实验",
    "hep-lat": "高能物理·格点",
    "hep-ph": "高能物理·唯象",
    "hep-th": "高能物理·理论",
    "math-ph": "数学物理",
    "nlin": "非线性科学",
    "nucl-ex": "核物理·实验",
    "nucl-th": "核物理·理论",
    "physics": "物理学",
    "quant-ph": "量子物理",
    "math": "数学",
    "cs": "计算机科学",
    "q-bio": "定量生物学",
    "q-fin": "定量金融",
    "stat": "统计学",
    "eess": "电子工程与系统科学",
    "econ": "经济学",
}

#: subject 的中文名，键是**全 slug**（`cs.CG` 与 `math.CG` 是两回事）。
SUBJECT_LABELS_ZH: dict[str, str] = {
    "astro-ph.CO": "宇宙学与河外天体物理",
    "astro-ph.EP": "地球与行星天体物理",
    "astro-ph.GA": "星系天体物理",
    "astro-ph.HE": "高能天体物理现象",
    "astro-ph.IM": "天体物理仪器与方法",
    "astro-ph.SR": "太阳与恒星天体物理",
    "cond-mat.dis-nn": "无序系统与神经网络",
    "cond-mat.mes-hall": "介观与纳米物理",
    "cond-mat.mtrl-sci": "材料科学",
    "cond-mat.other": "其他凝聚态",
    "cond-mat.quant-gas": "量子气体",
    "cond-mat.soft": "软凝聚态",
    "cond-mat.stat-mech": "统计力学",
    "cond-mat.str-el": "强关联电子",
    "cond-mat.supr-con": "超导",
    "nlin.AO": "适应与自组织系统",
    "nlin.CD": "混沌动力学",
    "nlin.CG": "元胞自动机与格子气",
    "nlin.PS": "斑图形成与孤子",
    "nlin.SI": "可积系统",
    "physics.acc-ph": "加速器物理",
    "physics.ao-ph": "大气与海洋物理",
    "physics.app-ph": "应用物理",
    "physics.atm-clus": "原子与分子团簇",
    "physics.atom-ph": "原子物理",
    "physics.bio-ph": "生物物理",
    "physics.chem-ph": "化学物理",
    "physics.class-ph": "经典物理",
    "physics.comp-ph": "计算物理",
    "physics.data-an": "数据分析与统计",
    "physics.ed-ph": "物理教育",
    "physics.flu-dyn": "流体力学",
    "physics.gen-ph": "普通物理",
    "physics.geo-ph": "地球物理",
    "physics.hist-ph": "物理学史与哲学",
    "physics.ins-det": "仪器与探测器",
    "physics.med-ph": "医学物理",
    "physics.optics": "光学",
    "physics.plasm-ph": "等离子体物理",
    "physics.pop-ph": "科普物理",
    "physics.soc-ph": "物理与社会",
    "physics.space-ph": "空间物理",
    "math.AC": "交换代数",
    "math.AG": "代数几何",
    "math.AP": "偏微分方程分析",
    "math.AT": "代数拓扑",
    "math.CA": "经典分析与常微分方程",
    "math.CO": "组合数学",
    "math.CT": "范畴论",
    "math.CV": "复变函数",
    "math.DG": "微分几何",
    "math.DS": "动力系统",
    "math.FA": "泛函分析",
    "math.GM": "一般数学",
    "math.GN": "一般拓扑",
    "math.GR": "群论",
    "math.GT": "几何拓扑",
    "math.HO": "数学史与综述",
    "math.IT": "信息论",
    "math.KT": "K 理论与同调",
    "math.LO": "数理逻辑",
    "math.MG": "度量几何",
    "math.MP": "数学物理",
    "math.NA": "数值分析",
    "math.NT": "数论",
    "math.OA": "算子代数",
    "math.OC": "最优化与控制",
    "math.PR": "概率论",
    "math.QA": "量子代数",
    "math.RA": "环与代数",
    "math.RT": "表示论",
    "math.SG": "辛几何",
    "math.SP": "谱理论",
    "math.ST": "统计理论",
    "cs.AI": "人工智能",
    "cs.AR": "硬件体系结构",
    "cs.CC": "计算复杂性",
    "cs.CE": "计算工程与科学",
    "cs.CG": "计算几何",
    "cs.CL": "计算语言学",
    "cs.CR": "密码学与安全",
    "cs.CV": "计算机视觉与模式识别",
    "cs.CY": "计算机与社会",
    "cs.DB": "数据库",
    "cs.DC": "分布式与并行计算",
    "cs.DL": "数字图书馆",
    "cs.DM": "离散数学",
    "cs.DS": "数据结构与算法",
    "cs.ET": "新兴技术",
    "cs.FL": "形式语言与自动机",
    "cs.GL": "综合文献",
    "cs.GR": "计算机图形学",
    "cs.GT": "计算博弈论",
    "cs.HC": "人机交互",
    "cs.IR": "信息检索",
    "cs.IT": "信息论",
    "cs.LG": "机器学习",
    "cs.LO": "计算机科学中的逻辑",
    "cs.MA": "多智能体系统",
    "cs.MM": "多媒体",
    "cs.MS": "数学软件",
    "cs.NA": "数值分析",
    "cs.NE": "神经与演化计算",
    "cs.NI": "网络与互联网架构",
    "cs.OH": "其他计算机科学",
    "cs.OS": "操作系统",
    "cs.PF": "性能",
    "cs.PL": "程序设计语言",
    "cs.RO": "机器人学",
    "cs.SC": "符号计算",
    "cs.SD": "声音",
    "cs.SE": "软件工程",
    "cs.SI": "社会与信息网络",
    "cs.SY": "系统与控制",
    "q-bio.BM": "生物大分子",
    "q-bio.CB": "细胞行为",
    "q-bio.GN": "基因组学",
    "q-bio.MN": "分子网络",
    "q-bio.NC": "神经元与认知",
    "q-bio.OT": "其他定量生物",
    "q-bio.PE": "种群与演化",
    "q-bio.QM": "定量方法",
    "q-bio.SC": "亚细胞过程",
    "q-bio.TO": "组织与器官",
    "q-fin.CP": "计算金融",
    "q-fin.EC": "经济学",
    "q-fin.GN": "一般金融",
    "q-fin.MF": "数理金融",
    "q-fin.PM": "投资组合管理",
    "q-fin.PR": "证券定价",
    "q-fin.RM": "风险管理",
    "q-fin.ST": "统计金融",
    "q-fin.TR": "交易与市场微结构",
    "stat.AP": "统计应用",
    "stat.CO": "统计计算",
    "stat.ME": "统计方法",
    "stat.ML": "机器学习（统计）",
    "stat.OT": "其他统计",
    "stat.TH": "统计理论",
    "eess.AS": "音频与语音处理",
    "eess.IV": "图像与视频处理",
    "eess.SP": "信号处理",
    "eess.SY": "系统与控制",
    "econ.EM": "计量经济学",
    "econ.GN": "一般经济学",
    "econ.TH": "理论经济学",
}

_LEAF_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,47}$")


def spine_categories() -> tuple[str, ...]:
    """全部骨架分类（archive.subject 或单层 archive）。"""
    out: list[str] = []
    for archive, subs in ARXIV_SPINE.items():
        if not subs:
            out.append(archive)
        out += [f"{archive}.{s}" for s in subs]
    return tuple(out)


_SPINE = frozenset(spine_categories())


def domain_label(domain: str, *, lang: str = "en") -> str:
    """人读名。查不到就返回 slug 本身 —— 显示一个 slug 不好看，但**不显示**
    更糟，而编一个名字最糟（本地叶的名字只有注册它的人知道）。"""
    d = (domain or "").strip()
    if not d:
        return ""
    subjects = SUBJECT_LABELS_ZH if lang == "zh" else SUBJECT_LABELS
    archives = ARCHIVE_LABELS_ZH if lang == "zh" else ARCHIVE_LABELS
    if d in subjects:
        return subjects[d]
    if d in archives:
        return archives[d]
    parts = split_leaf(d)
    if parts is not None:
        return parts[1].replace("-", " ")
    return d


def domain_catalog(state: Any = None, *, lang: str = "en") -> tuple[dict, ...]:
    """给"让人挑域"的界面用的分组目录：archive → 它底下的分类。

    含 org 已注册的本地叶（挂在各自的骨架父下），所以本组自己长出来的方向
    在选择界面上和骨架分类一样可选 —— 不然本地叶就成了只有晋升流程看得见
    的暗词表。
    """
    leaves_by_parent: dict[str, list[str]] = {}
    if state is not None:
        for leaf in sorted(_local_leaves(state)):
            parts = split_leaf(leaf)
            if parts is not None:
                leaves_by_parent.setdefault(parts[0], []).append(leaf)

    out: list[dict] = []
    for archive, subs in ARXIV_SPINE.items():
        children: list[dict] = []
        if not subs:
            children.append({"domain": archive, "label": domain_label(archive, lang=lang),
                             "kind": "spine"})
        for sub in subs:
            slug = f"{archive}.{sub}"
            children.append({"domain": slug, "label": domain_label(slug, lang=lang),
                             "kind": "spine"})
        for parent in ([archive] if not subs else [f"{archive}.{s}" for s in subs]):
            for leaf in leaves_by_parent.get(parent, ()):
                children.append({"domain": leaf, "label": domain_label(leaf, lang=lang),
                                 "kind": "leaf", "parent": parent})
        archive_labels = ARCHIVE_LABELS_ZH if lang == "zh" else ARCHIVE_LABELS
        out.append({"archive": archive, "label": archive_labels.get(archive, archive),
                    "categories": tuple(children)})
    return tuple(out)


@dataclass
class DomainVerdict:
    """一次域校验的结果。`registrable` 表示"骨架父合法 + 叶是新的"：
    check 阶段放行，promote 时随人批一起注册。"""

    domain: str
    status: str            # spine / leaf / registrable / invalid
    error: str = ""
    suggestions: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status in ("spine", "leaf", "registrable")


def split_leaf(domain: str) -> tuple[str, str] | None:
    """`physics.comp-ph/mlip-robustness` → (骨架父, 叶名)；不是叶语法返 None。"""
    if "/" not in domain:
        return None
    parent, _, leaf = domain.partition("/")
    return (parent.strip(), leaf.strip())


def validate_domain(state: Any, domain: str) -> DomainVerdict:
    """校验一个域。拒收时给最近匹配 + 挂叶语法 —— 别逼调用方猜。"""
    d = (domain or "").strip()
    if not d:
        return DomainVerdict(d, "invalid", "域为空。晋升必须选域 —— 域是送达地址，"
                             "没有地址的知识不构成资产。", _suggest(""))
    if d in _SPINE:
        return DomainVerdict(d, "spine")

    parts = split_leaf(d)
    if parts is not None:
        parent, leaf = parts
        if parent not in _SPINE:
            return DomainVerdict(
                d, "invalid",
                f"叶 {leaf!r} 的骨架父 {parent!r} 不在 arXiv 分类里 —— 本地叶必须"
                f"挂在骨架节点下，不许悬空。", _suggest(parent))
        if not _LEAF_RE.match(leaf):
            return DomainVerdict(
                d, "invalid",
                f"叶名 {leaf!r} 不合法：小写字母/数字/连字符，2–48 字符。")
        if d in _local_leaves(state):
            return DomainVerdict(d, "leaf")
        return DomainVerdict(d, "registrable")

    return DomainVerdict(
        d, "invalid",
        f"{d!r} 不在注册表里。可选：骨架分类（arXiv 词表，如 cond-mat.stat-mech）、"
        f"已注册本地叶，或用 `<骨架父>/<新叶名>` 语法挂新叶（随晋升人批一起注册）。",
        _suggest(d))


def register_leaf(state: Any, *, domain: str, description: str,
                  approved_by: str, at: str) -> dict:
    """注册一片本地叶。**随晋升人批背书**（approved_by 必填），不单开审批流。

    幂等：已注册返 already_registered，不报错 —— 同一批次晋升多张同域卡是常态。
    """
    verdict = validate_domain(state, domain)
    if verdict.status == "leaf":
        return {"status": "success", "code": "already_registered", "domain": domain}
    if verdict.status != "registrable":
        return {"status": "error", "code": "not_registrable",
                "error": verdict.error or f"{domain!r} 不是可注册的叶",
                "suggestions": list(verdict.suggestions)}
    if not (approved_by or "").strip():
        return {"status": "error", "code": "approval_required",
                "error": "挂新叶必须有人批背书（approved_by）—— 词表治理和知识准入"
                         "走同一道人批"}
    rec = {"domain": domain, "parent": split_leaf(domain)[0],
           "description": (description or "").strip(),
           "approved_by": approved_by, "at": at}
    path = _leaves_path(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"status": "success", "code": "registered", "domain": domain}


def ancestors(domain: str) -> tuple[str, ...]:
    """送达走查用的上行链：叶 → 骨架分类 → archive。最细在前。

    physics.comp-ph/mlip-robustness → (physics.comp-ph/mlip-robustness,
                                       physics.comp-ph, physics)
    """
    d = (domain or "").strip()
    if not d:
        return ()
    chain = [d]
    parts = split_leaf(d)
    if parts is not None:
        chain.append(parts[0])
        d = parts[0]
    if "." in d:
        chain.append(d.split(".", 1)[0])
    return tuple(chain)


def suggest_from_evidence(state: Any, claim_id: str) -> tuple[str, ...]:
    """从证据链的 arXiv 分类机械建议主域。**查得到就给，查不到不猜。**

    书目 chunk 的 metadata 若带 categories（arXiv 摘取时存的），按出现频次
    给建议；没有元数据就返回空 —— 猜错的分域比没有分域更糟。
    """
    try:
        from core.kb_promotion import evidence_closure
    except Exception:
        return ()
    counts: dict[str, int] = {}
    for ev in evidence_closure(state, claim_id):
        if not ev.startswith("chunk_"):
            continue
        try:
            chunk = state.get_kb_record("chunks", ev) or {}
        except Exception:
            continue
        for cat in (chunk.get("arxiv_categories") or ()):
            c = str(cat).strip()
            if c in _SPINE:
                counts[c] = counts.get(c, 0) + 1
    return tuple(sorted(counts, key=counts.get, reverse=True)[:3])


# ── 盘面 ────────────────────────────────────────────────────────────────────


def _leaves_path(state: Any):
    from core import paths

    return paths.org_root() / "domain_leaves.jsonl"


def _local_leaves(state: Any) -> frozenset[str]:
    path = _leaves_path(state)
    if not path.exists():
        return frozenset()
    out = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.add(str(json.loads(line).get("domain") or ""))
        except json.JSONDecodeError:
            continue
    return frozenset(o for o in out if o)


def _suggest(query: str) -> tuple[str, ...]:
    """最近匹配：前缀 / 子串优先，兜底给常用分类。"""
    q = (query or "").lower().strip()
    cats = spine_categories()
    if q:
        hits = [c for c in cats if q in c.lower()]
        if hits:
            return tuple(hits[:SUGGEST_K])
    # 平台的主战场：计算科学 + 材料 + ML
    return ("physics.comp-ph", "cond-mat.stat-mech", "cond-mat.mtrl-sci",
            "cs.LG", "stat.ML", "physics.chem-ph")[:SUGGEST_K]
