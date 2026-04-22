from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
import time

def test_dropdown():
    # 1. Setup the Chrome Driver
    options = webdriver.ChromeOptions()
    # options.add_argument('--headless') # Uncomment to run without a window
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)

    try:
        # 2. Go to your local Flask app
        driver.get("http://127.0.0.1:5000")
        time.sleep(1) # Wait for page load

        # 3. Locate the "Collections" dropdown parent
        dropdown_parent = driver.find_element(By.CLASS_NAME, "dropdown")
        dropdown_content = driver.find_element(By.CLASS_NAME, "dropdown-content")

        # Check initial state
        print(f"Initial display status: {dropdown_content.is_displayed()}")

        # 4. Perform the hover action
        actions = ActionChains(driver)
        actions.move_to_element(dropdown_parent).perform()
        
        # Give the browser a split second to update the UI
        time.sleep(0.5)

        # 5. Analyze the result
        is_visible = dropdown_content.is_displayed()
        z_index = dropdown_content.value_of_css_property("z-index")
        display_type = dropdown_content.value_of_css_property("display")

        print("--- Diagnostic Report ---")
        print(f"Is content visible to user? {is_visible}")
        print(f"Computed Display: {display_type}")
        print(f"Z-Index value: {z_index}")

        if not is_visible:
            print("\nSUGGESTION: The hover isn't triggering 'display: block'.")
            print("Check if there is a gap between the navbar and the dropdown content.")

    except Exception as e:
        print(f"Test failed with error: {e}")
    finally:
        driver.quit()

if __name__ == "__main__":
    test_dropdown()