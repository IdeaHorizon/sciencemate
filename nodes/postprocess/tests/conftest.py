"""postprocess 测试目录的 pytest 配置。"""


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_sandbox: 不用 test_figure_contract 里的假沙箱，走真 execute_python（沙箱 + 高危闸在场）",
    )
