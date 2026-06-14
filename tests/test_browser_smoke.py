import pytest


pytestmark = pytest.mark.browser


@pytest.fixture()
def browser_driver():
    try:
        from selenium import webdriver
    except Exception as exc:
        pytest.skip(f"Selenium webdriver import unavailable: {exc}")

    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1440,900")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")

    try:
        driver = webdriver.Chrome(options=options)
    except Exception as exc:
        pytest.skip(f"Chrome WebDriver unavailable: {exc}")

    yield driver
    driver.quit()


def _login_from_ui(driver, base_url, username, password):
    from selenium.webdriver.common.by import By

    driver.get(f"{base_url}/login")

    username_input = driver.find_element(By.ID, "username")
    password_input = driver.find_element(By.ID, "password")

    username_input.clear()
    username_input.send_keys(username)

    password_input.clear()
    password_input.send_keys(password)

    driver.find_element(By.CSS_SELECTOR, "button[type='submit']").click()


def test_home_guest_shows_login_link(live_server, browser_driver):
    from selenium.webdriver.common.by import By

    browser_driver.get(f"{live_server}/")

    links = browser_driver.find_elements(By.LINK_TEXT, "Login")
    assert links, "Expected Login link in guest navigation"


def test_admin_login_shows_admin_nav(live_server, browser_driver, seed_users):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    _login_from_ui(
        browser_driver,
        live_server,
        seed_users["admin"]["username"],
        seed_users["admin"]["password"],
    )

    WebDriverWait(browser_driver, 10).until(EC.url_contains("/"))

    browser_driver.get(f"{live_server}/")

    assert browser_driver.find_elements(By.LINK_TEXT, "Add Cards")
    assert '/market/dashboard' in browser_driver.page_source


def test_market_dashboard_requires_login_browser(live_server, browser_driver):
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    browser_driver.get(f"{live_server}/market/dashboard")

    WebDriverWait(browser_driver, 10).until(EC.url_contains("/login"))
    assert "/login" in browser_driver.current_url


def test_user_home_search_routes_to_trade_binder(live_server, browser_driver, seed_users, seed_cards):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    _login_from_ui(
        browser_driver,
        live_server,
        seed_users["user"]["username"],
        seed_users["user"]["password"],
    )

    browser_driver.get(f"{live_server}/")

    search_input = browser_driver.find_element(By.ID, "home-card-search")
    search_input.clear()
    search_input.send_keys("Sol")

    browser_driver.find_element(By.CSS_SELECTOR, ".catalog-search button[type='submit']").click()

    WebDriverWait(browser_driver, 10).until(EC.url_contains("/binder/trades"))

    assert "q=Sol" in browser_driver.current_url
    assert "Sol Ring" in browser_driver.page_source
