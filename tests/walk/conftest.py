def pytest_configure(config):
    config.addinivalue_line("markers", "chromium: needs the locked Playwright Chromium; CI only")
