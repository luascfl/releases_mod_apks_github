# -*- coding: utf-8 -*-
"""
Advanced APK Downloader - Native Download Edition
"""

import os
import sys
import shutil
import time
import logging
import asyncio
import nest_asyncio
import random
import re
import hashlib
import subprocess
import tempfile
import glob
import base64
import requests
from urllib.parse import urlparse, unquote
from abc import ABC, abstractmethod
from typing import List, Dict, Any

try:
    from pyaxmlparser import APK
except ImportError:
    APK = None

# --- Configurações Locais ---
HEADLESS_MODE = False
BASE_DIR = os.getcwd()
LOGS_DIR = os.path.join(BASE_DIR, 'logs')
# Pasta temporária para downloads do DrissionPage
TEMP_DOWNLOAD_DIR = os.path.join(BASE_DIR, 'temp_downloads')
DOWNLOADS_DIR = BASE_DIR

for d in [LOGS_DIR, TEMP_DOWNLOAD_DIR]:
    os.makedirs(d, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S',
    handlers=[logging.FileHandler(os.path.join(LOGS_DIR, 'scraper.log')), logging.StreamHandler()]
)
logger = logging.getLogger("APK_Downloader")

USER_AGENTS = [
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
]

# Imports de Automação
try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium_stealth import stealth
    from webdriver_manager.chrome import ChromeDriverManager
    from DrissionPage import ChromiumPage, ChromiumOptions
except ImportError:
    logger.error("Instale as dependências: pip install selenium selenium-stealth webdriver-manager DrissionPage nest-asyncio aiohttp pyaxmlparser")
    sys.exit(1)

nest_asyncio.apply()

class APKScraper:
    def __init__(self):
        self.page = None

    async def init_browser(self):
        try:
            co = ChromiumOptions()
            co.set_argument('--no-sandbox')
            co.set_argument('--disable-gpu')
            if os.path.exists('/usr/bin/google-chrome'):
                co.set_browser_path('/usr/bin/google-chrome')
            co.set_user_agent(random.choice(USER_AGENTS))
            
            # Configura pasta de download específica para o DrissionPage
            co.set_download_path(TEMP_DOWNLOAD_DIR)
            
            self.page = ChromiumPage(co)
            logger.info("✅ Navegador iniciado")
        except Exception as e:
            logger.error(f"❌ Falha ao iniciar navegador: {e}")
            raise

    async def process_liteapks(self, app_config: Dict):
        url = app_config['url']
        app_name = app_config['name']
        folder = app_config['folder']
        
        logger.info(f"🌐 Navegando para {app_name}: {url}")
        self.page.get(url)
        
        # 1. Botão Download Inicial
        btn = self.page.ele("text:Download")
        if btn:
            btn.click(by_js=True)
            await asyncio.sleep(5)
            
            # 2. Busca link na página de download
            content = self.page.html
            links = re.findall(r'https?://liteapks\.com/download/[^\s<>" ]+', content)
            if links:
                logger.info(f"👉 Acessando página de versões: {links[0]}")
                self.page.get(links[0])
                await asyncio.sleep(5)
                
                # 2.5 Na página de versões, busca o link específico da primeira versão (MOD)
                version_links = re.findall(r'https?://liteapks\.com/download/[^\s<>"]+/\d+', self.page.html)
                if version_links:
                    logger.info(f"👉 Acessando link da versão: {version_links[0]}")
                    self.page.get(version_links[0])
                    await asyncio.sleep(15) # Espera o timer do LiteAPKs
                
                # 3. Extrai o link final (href ou data-href)
                logger.info("⏳ Aguardando botão de download final...")
                await asyncio.sleep(10)  # Espera o carregamento do botão
                
                final_url = self.extract_final_download_url(self.page.html)
                if not final_url:
                    # Fallback via JS para páginas com renderização dinâmica
                    final_url = self.page.run_js("""
                        let el = document.querySelector('#download-loaded-link, a.download, a[download], a[href*=\".apk\"]');
                        if (!el) return null;
                        
                        let href = el.getAttribute('href') || el.href;
                        if (href && href.startsWith('http') && !href.includes('#!')) {
                            return href;
                        }
                        
                        let dataHref = el.getAttribute('data-href');
                        if (dataHref) {
                            try {
                                return atob(dataHref);
                            } catch (err) {
                                return null;
                            }
                        }
                        
                        return null;
                    """)
                
                if final_url:
                    final_url = unquote(str(final_url)).replace('&amp;', '&').strip()
                    logger.info(f"🚀 Link direto detectado: {final_url}")

                    downloaded_file = await asyncio.to_thread(self.download_apk_with_requests, final_url)
                    if downloaded_file:
                        return await self.wait_and_move_download(folder, app_name, downloaded_file)

                    logger.error("❌ Falha no download direto do APK.")
                    return False
                
                logger.error("❌ Não foi possível encontrar o link final decodificado.")
        return False
                        

    def extract_final_download_url(self, page_html: str) -> str:
        if not page_html:
            return ""

        # Tenta primeiro links diretos já prontos no HTML
        direct_patterns = [
            r'id="download-loaded-link"[^>]*href="([^"]+)"',
            r'class="[^"]*\bdownload\b[^"]*"[^>]*href="([^"]+)"',
            r'href="(https?://[^"\s>]+\.apk[^"\s>]*)"',
        ]

        for pattern in direct_patterns:
            match = re.search(pattern, page_html, flags=re.IGNORECASE | re.DOTALL)
            if not match:
                continue

            candidate = unquote(match.group(1)).replace('&amp;', '&').strip()
            if candidate.startswith('http') and '#!' not in candidate:
                return candidate

        # Fallback para data-href base64
        data_href_match = re.search(r'data-href="([^"]+)"', page_html, flags=re.IGNORECASE)
        if not data_href_match:
            return ""

        try:
            decoded = base64.b64decode(data_href_match.group(1)).decode('utf-8')
            decoded = unquote(decoded).replace('&amp;', '&').strip()
            if decoded.startswith('http'):
                return decoded
        except Exception:
            return ""

        return ""

    def get_browser_cookies(self) -> Dict[str, str]:
        cookies: Dict[str, str] = {}
        if not self.page:
            return cookies

        try:
            raw_cookies = self.page.cookies(all_domains=True, all_info=True)
        except Exception as exc:
            logger.warning(f"⚠️ Falha ao ler cookies do navegador: {exc}")
            return cookies

        if isinstance(raw_cookies, dict):
            for name, value in raw_cookies.items():
                if name and value is not None:
                    cookies[str(name)] = str(value)
            return cookies

        if isinstance(raw_cookies, (list, tuple, set)):
            for item in raw_cookies:
                if not isinstance(item, dict):
                    continue
                name = item.get('name')
                value = item.get('value')
                if name and value is not None:
                    cookies[str(name)] = str(value)

        return cookies

    def download_apk_with_requests(self, file_url: str) -> str:
        parsed = urlparse(file_url)
        file_name = os.path.basename(unquote(parsed.path)) or f"apk_{int(time.time())}.apk"
        if not file_name.lower().endswith('.apk'):
            file_name = f"{file_name}.apk"

        target_path = os.path.join(TEMP_DOWNLOAD_DIR, file_name)
        part_path = f"{target_path}.part"
        referer = self.page.url if self.page else "https://liteapks.com/"

        headers = {
            'User-Agent': random.choice(USER_AGENTS),
            'Referer': referer,
            'Accept': '*/*',
            'Connection': 'keep-alive',
        }
        cookies = self.get_browser_cookies()

        last_error = None
        for attempt in range(1, 4):
            try:
                if os.path.exists(part_path):
                    os.remove(part_path)

                logger.info(f"📥 Baixando APK (tentativa {attempt}/3)...")
                with requests.get(
                    file_url,
                    headers=headers,
                    cookies=cookies if cookies else None,
                    stream=True,
                    allow_redirects=True,
                    timeout=(30, 180),
                ) as response:
                    response.raise_for_status()
                    with open(part_path, 'wb') as apk_file:
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            if chunk:
                                apk_file.write(chunk)

                os.replace(part_path, target_path)
                logger.info(f"✅ Download concluído: {target_path}")
                return target_path
            except Exception as exc:
                last_error = exc
                logger.warning(f"⚠️ Tentativa {attempt} falhou: {exc}")
                time.sleep(attempt * 2)

        if os.path.exists(part_path):
            os.remove(part_path)
        logger.error(f"❌ Download falhou após 3 tentativas: {last_error}")
        return ""

    async def wait_and_move_download(self, target_folder: str, app_name: str, downloaded_file: str = ""):
        timeout = 300
        start_time = time.time()

        dest_dir = os.path.join(BASE_DIR, target_folder)
        os.makedirs(dest_dir, exist_ok=True)

        def move_file(latest_apk: str) -> bool:
            version = self.extract_version_from_apk(latest_apk)
            version = re.sub(r'[^\w\.-]', '', version)

            final_path = os.path.join(dest_dir, f"{app_name}_v{version}.apk")
            if os.path.exists(final_path):
                logger.info(f"⏭️ Versão {version} já existe.")
                os.remove(latest_apk)
                return True

            shutil.move(latest_apk, final_path)
            logger.info(f"🚀 Movido para: {final_path}")
            return True

        if downloaded_file and os.path.exists(downloaded_file):
            logger.info(f"✨ APK baixado diretamente: {downloaded_file}")
            return move_file(downloaded_file)

        logger.info(f"📂 Monitorando {TEMP_DOWNLOAD_DIR}...")
        while time.time() - start_time < timeout:
            # Verifica arquivos na pasta temporária
            apks = glob.glob(os.path.join(TEMP_DOWNLOAD_DIR, "*.apk"))
            crdownloads = glob.glob(os.path.join(TEMP_DOWNLOAD_DIR, "*.crdownload"))
            tmp_files = glob.glob(os.path.join(TEMP_DOWNLOAD_DIR, "*.tmp"))

            if not crdownloads and not tmp_files and apks:
                latest_apk = max(apks, key=os.path.getctime)
                logger.info(f"✨ Novo APK encontrado: {latest_apk}")
                return move_file(latest_apk)

            await asyncio.sleep(2)

        logger.error("❌ Timeout aguardando download.")
        return False

    def extract_version_from_apk(self, file_path: str) -> str:
        if not APK: return "unknown"
        try:
            apk = APK(file_path)
            return apk.version_name or apk.version_code or "unknown"
        except: return "unknown"

    async def cleanup(self):
        if self.page: self.page.quit()
        if os.path.exists(TEMP_DOWNLOAD_DIR):
            shutil.rmtree(TEMP_DOWNLOAD_DIR, ignore_errors=True)

async def main():
    print("="*50)
    print(f"🚀 APK BUILDER (Native Download Mode)")
    print("="*50)

    apps = [
        {"name": "Endel", "folder": "Endel", "repo": "endel", "url": "https://liteapks.com/endel.html"},
        {"name": "CamScanner", "folder": "CamScanner", "repo": "camscanner", "url": "https://liteapks.com/camscanner.html"}
    ]

    scraper = APKScraper()
    try:
        await scraper.init_browser()
        for app in apps:
            print(f"\n📱 {app['name']}...")
            success = await scraper.process_liteapks(app)
            if not success: logger.error(f"❌ Falha {app['name']}")
    finally:
        await scraper.cleanup()
        print("\n🏁 Finalizado.")
        print("\n🚀 Iniciando Push para GitHub (Subfolders + Releases)...")
        try:
            subprocess.run(["./create_and_push_repo.sh", "push"], check=True)
            print("✅ Push e Releases concluídos!")
            print("\n🔗 LINKS DAS ÚLTIMAS RELEASES:")
        except subprocess.CalledProcessError as exc:
            logger.error(f"❌ Falha no push/repos: {exc}")

if __name__ == "__main__":
    asyncio.run(main())
