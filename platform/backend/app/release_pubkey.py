"""自更新的信任根 —— 发布签名的**公钥**。

这把公钥对应发布机上的私钥（`~/.harness-framework/release-signing/ed25519.key`，
见 `scripts/package/release_keys.py`）。装好的应用只认由这把私钥签的载荷。

换钥匙 = 已装出去的应用再也收不到更新。所以这个常量几乎永远不动；真要换，
先发一版带**新旧两把**公钥的应用，等所有人都升上去，再撤旧的。
"""

RELEASE_PUBLIC_KEY_B64 = "mRTzy4dwiDMJqltX7xRMKTSENWKs1Lv4Ler7PrLL+nc="
