"""缺一个能力槽这件事有**两个读者**，所以目录得有两句话。

`absence_note` 是注入给消费方节点的：「审图工具不在你的工具面上……你什么都
不用做，也不要为这件事重跑 —— 平台配没配审图模型不是你能改的事。」对一个
正在跑的节点，这句话完全正确。

但『设置 → 模型』那一页的读者恰恰是**唯一能把这个槽填上的人**。2026-08-31
实测：一位研究员在那一页读到这句话，同时看到下拉是灰的、下面五条连接全是
只读的，于是认为平台不支持配审图模型 —— 而后端从来没拦过他（建个人连接
不查管理权限，他自己注册一条勾上就能用）。一份文案同时服务两个相反的读者，
必然对其中一个撒谎。

于是分成 `absence_note`（给节点）和 `absence_impact`（给人）。这个文件钉住
后者**每个角色都有、且送得到 API 面上** —— 送不到的话，前端读到 undefined，
那一页又变回只有一个空槽。
"""

from app.services.model_role_catalog import catalog


def test_每个角色都带一句写给人的缺席影响():
    # 扫盘，不写名单：以后加一个角色，忘了写这句话，这条测试就红。
    for spec in catalog():
        assert spec.absence_impact, f"角色 {spec.id!r} 没有 absence_impact —— 设置页上它会是个哑槽"


def test_给人的那句话不是给节点的那句话的复制():
    for spec in catalog():
        assert spec.absence_impact != spec.absence_note, (
            f"角色 {spec.id!r} 的两句话一模一样 —— 那就是两个读者又合回了一份文案"
        )


def test_缺席影响送得到_API_面上():
    # `GET /settings/model-roles` 直接 `**item.to_public_dict()`。这里断的是
    # 那一层：字段留在 dataclass 里而没进投影，前端拿到的就是 undefined。
    for spec in catalog():
        public = spec.to_public_dict()
        assert public["absence_impact"] == spec.absence_impact
