import asyncio
import aiohttp
import pwinput
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import os
import re
import logging
from urllib.parse import unquote


class CourseMaterialsDownloader:
    def __init__(self, student_id, password, base_dir):
        self.chrome_options = None
        self.student_id = student_id
        self.password = password
        self.base_dir = base_dir
        self.session = None
        self.cookies = {}
        self.setup_chrome_options()
        self.setup_logging()

    def setup_logging(self):
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger(__name__)

    def setup_chrome_options(self):
        self.chrome_options = Options()
        self.chrome_options.add_argument('--headless')
        self.chrome_options.add_argument('--disable-gpu')
        self.chrome_options.add_argument('--no-sandbox')
        self.chrome_options.add_argument('--window-size=1920,1080')
        self.chrome_options.page_load_strategy = 'eager'

    def get_cookies_from_selenium(self, driver):
        """Extract cookies from Selenium session after login"""
        cookies = driver.get_cookies()
        return {cookie['name']: cookie['value'] for cookie in cookies}

    async def init_session(self, cookies):
        """Initialize aiohttp session with cookies from successful login"""
        timeout = aiohttp.ClientTimeout(total=300)

        # Convert Selenium cookies to aiohttp format
        cookie_string = '; '.join([f"{name}={value}" for name, value in cookies.items()])

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': '*/*',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Cookie': cookie_string
        }

        self.session = aiohttp.ClientSession(
            timeout=timeout,
            headers=headers,
            cookie_jar=aiohttp.CookieJar()
        )

    async def close_session(self):
        if self.session:
            await self.session.close()

    def login_portal(self):
        try:
            driver = webdriver.Chrome(options=self.chrome_options)
            driver.get("https://portal.aiub.edu/")
            self.logger.info("Loading portal.aiub.edu")

            username_field = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.ID, "username"))
            )
            password_field = driver.find_element(By.ID, "password")

            driver.execute_script(
                f'arguments[0].value = "{self.student_id}"; arguments[1].value = "{self.password}";',
                username_field, password_field
            )

            login_button = driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
            login_button.click()

            # Wait for login to complete
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div.StudentCourseList"))
            )
            self.logger.info("Login successful")

            # Store cookies after successful login
            self.cookies = self.get_cookies_from_selenium(driver)
            return driver

        except Exception as e:
            self.logger.error(f"Login error: {e}")
            if 'driver' in locals():
                driver.quit()
            return None

    async def download_file(self, url, file_path, chunk_size=8192):
        try:
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            temp_path = file_path + '.temp'

            # Get the download URL and any redirect cookies
            async with self.session.get(url, allow_redirects=False) as response:
                if response.status == 302:  # Handle redirect
                    actual_url = response.headers.get('Location')
                    if not actual_url:
                        self.logger.error("No redirect URL found")
                        return False
                else:
                    actual_url = url

            # Download the file from the actual URL
            async with self.session.get(actual_url, allow_redirects=True) as response:
                if response.status != 200:
                    self.logger.error(f"Failed to download {os.path.basename(file_path)} (Status: {response.status})")
                    return False

                try:
                    with open(temp_path, 'wb') as f:
                        async for chunk in response.content.iter_chunked(chunk_size):
                            if chunk:
                                f.write(chunk)

                    # Move temp file to final location
                    os.replace(temp_path, file_path)
                    self.logger.info(f"Successfully downloaded: {os.path.basename(file_path)}")
                    return True

                except Exception as e:
                    self.logger.error(f"Error saving file {os.path.basename(file_path)}: {e}")
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                    return False

        except Exception as e:
            self.logger.error(f"Error downloading {os.path.basename(file_path)}: {e}")
            return False

    async def download_course_materials(self, driver, course_name, course_dir):
        try:
            materials = driver.execute_script("""
                return Array.from(document.querySelectorAll('#notesTab table.table tbody tr:not(:first-child)')).map(row => {
                    const link = row.querySelector('td:nth-child(2) a');
                    return {
                        name: link.textContent.trim(),
                        url: link.href,
                        size: row.querySelector('td:nth-child(3)').textContent.trim()
                    };
                });
            """)

            download_tasks = []
            for material in materials:
                clean_file_name = unquote(re.sub(r'[<>:"/\\|?*]', '_', material['name']))
                file_path = os.path.join(course_dir, clean_file_name)

                if not os.path.exists(file_path):
                    download_tasks.append(self.download_file(material['url'], file_path))
                else:
                    self.logger.info(f"Skipped existing: {clean_file_name}")

            if download_tasks:
                results = await asyncio.gather(*download_tasks, return_exceptions=True)
                success_count = sum(1 for r in results if r is True)
                self.logger.info(f"Downloaded {success_count} of {len(download_tasks)} files for {course_name}")
            else:
                self.logger.info("No new files to download")

        except Exception as e:
            self.logger.error(f"Error downloading materials: {e}")

    def get_course_sections(self, driver):
        try:
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div.row.StudentCourseList"))
            )

            script = """
            return Array.from(document.querySelectorAll('div.panel.panel-primary')).map(panel => {
                const courseText = panel.querySelector('div.panel-body.course-list-panel').textContent.trim();
                const notesLink = Array.from(panel.querySelectorAll('a')).find(link => 
                    link.href.includes('#notesTab'))?.href;
                return [courseText, notesLink];
            });
            """
            courses = driver.execute_script(script)

            return {
                re.search(r'-\s*(.+?)\s*\[', course[0]).group(1).strip(): course[1]
                for course in courses
                if course[1] and re.search(r'-\s*(.+?)\s*\[', course[0])
            }
        except Exception as e:
            self.logger.error(f"Error getting courses: {e}")
            return {}

    async def process_course(self, driver, course_name, link):
        try:
            self.logger.info(f"\nProcessing: {course_name}")
            clean_course_name = re.sub(r'[<>:"/\\|?*]', '_', course_name)
            course_dir = os.path.join(self.base_dir, clean_course_name)
            os.makedirs(course_dir, exist_ok=True)

            driver.get(link)
            await asyncio.sleep(2)

            notes_tab = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.ID, "notesTab"))
            )

            await self.download_course_materials(driver, course_name, course_dir)

        except Exception as e:
            self.logger.error(f"Error processing {course_name}: {e}")

    async def main(self):
        driver = self.login_portal()
        if not driver:
            return

        try:
            # Initialize session with cookies from successful login
            await self.init_session(self.cookies)
            courses = self.get_course_sections(driver)

            self.logger.info("\nFound courses:")
            for course in courses:
                self.logger.info(f"- {course}")

            for course_name, link in courses.items():
                await self.process_course(driver, course_name, link)

        finally:
            await self.close_session()
            driver.quit()
            self.logger.info("\nDownload complete")
if __name__ == "__main__":
    STUDENT_ID = input("Enter student ID: ")
      # Replace with your ID
    PASSWORD = pwinput.pwinput(prompt="Password:", mask="*")
    BASE_DIR = "<<DIRECTORY>>"  # Download Directory

    downloader = CourseMaterialsDownloader(STUDENT_ID, PASSWORD, BASE_DIR)
    asyncio.run(downloader.main())

    # REQUIRED LIBRARIES :
    # pip install pwinput
    # pip install selenium
    # pip install aiohttp selenium
    # pip install chromedriver_autoinstaller
