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
                
                # 3. Clica no botão final ou extrai o data-href
                logger.info("⏳ Aguardando botão de download final...")
                await asyncio.sleep(10) # Espera o carregamento do botão
                
                # Tenta extrair o link real via JS
                js_get_real_url = """
                    let el = document.querySelector('a.download');
                    if (el) {
                        // Se o href já for um link real (não #!), usa ele
                        if (el.href && !el.href.includes('#!') && el.href.startsWith('http')) {
                            return el.href;
                        }
                        // Caso contrário, tenta decodificar o data-href
                        if (el.getAttribute('data-href')) {
                            return atob(el.getAttribute('data-href'));
                        }
                    }
                    return null;
                """
                
                final_url = self.page.run_js(js_get_real_url)
                
                if final_url:
                    logger.info(f"🚀 Link direto (decodificado): {final_url}")
                    # Inicia download nativo do arquivo real
                    self.page.download(final_url, TEMP_DOWNLOAD_DIR)
                    return await self.wait_and_move_download(folder, app_name)
                else:
                    logger.error("❌ Não foi possível encontrar o link final decodificado.")
        return False
                        
        return False

    async def wait_and_move_download(self, target_folder: str, app_name: str):
        timeout = 300
        start_time = time.time()
        
        dest_dir = os.path.join(BASE_DIR, target_folder)
        os.makedirs(dest_dir, exist_ok=True)

        logger.info(f"📂 Monitorando {TEMP_DOWNLOAD_DIR}...")
        
        while time.time() - start_time < timeout:
            # Verifica arquivos na pasta temporária
            apks = glob.glob(os.path.join(TEMP_DOWNLOAD_DIR, "*.apk"))
            crdownloads = glob.glob(os.path.join(TEMP_DOWNLOAD_DIR, "*.crdownload"))
            tmp_files = glob.glob(os.path.join(TEMP_DOWNLOAD_DIR, "*.tmp"))
            
            if not crdownloads and not tmp_files and apks:
                latest_apk = max(apks, key=os.path.getctime)
                logger.info(f"✨ Novo APK encontrado: {latest_apk}")
                
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
            subprocess.run(["./create_and_push_repo.sh", "push-subfolders-releases"], check=True)
            print("✅ Push e Releases concluídos!")
            print("\n🔗 LINKS DAS ÚLTIMAS RELEASES:")
        except subprocess.CalledProcessError as exc:
            logger.error(f"❌ Falha no push/repos: {exc}")

if __name__ == "__main__":
    asyncio.run(main())
