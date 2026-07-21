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

ALREADY_PUBLISHED = "__ALREADY_PUBLISHED__"

for d in [LOGS_DIR, TEMP_DOWNLOAD_DIR]:
    os.makedirs(d, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S',
    handlers=[logging.FileHandler(os.path.join(LOGS_DIR, 'scraper.log')), logging.StreamHandler()]
)
logger = logging.getLogger("APK_Downloader")

class ConsoleProgressBar:
    def __init__(self, label: str, total_size: int = 0, width: int = 28):
        self.label = label
        self.total_size = total_size
        self.width = width
        self.current_bytes = 0
        self.last_render = ""
        self.disabled = not sys.stdout.isatty()

    def update(self, current_bytes: int):
        self.current_bytes = current_bytes
        if self.disabled:
            return

        current_mb = current_bytes / (1024 * 1024)
        if self.total_size:
            total_mb = self.total_size / (1024 * 1024)
            percent = min(current_bytes / self.total_size, 1)
            filled = int(self.width * percent)
            bar = "█" * filled + "·" * (self.width - filled)
            line = f"\r{self.label}: [{bar}] {percent * 100:6.2f}% ({current_mb:.2f}/{total_mb:.2f} MB)"
        else:
            line = f"\r{self.label}: {current_mb:.2f} MB"

        if line != self.last_render:
            sys.stdout.write(line)
            sys.stdout.flush()
            self.last_render = line

    def finish(self):
        if self.disabled:
            return
        self.update(self.total_size or self.current_bytes)
        sys.stdout.write("\n")
        sys.stdout.flush()
        self.last_render = ""


class ProgressFile:
    def __init__(self, file_path: str, label: str, total_size: int):
        self._fh = open(file_path, "rb")
        self.total_size = total_size
        self.sent_bytes = 0
        self.progress = ConsoleProgressBar(label, total_size)

    def read(self, size: int = -1):
        chunk = self._fh.read(size)
        if chunk:
            self.sent_bytes += len(chunk)
            self.progress.update(self.sent_bytes)
        return chunk

    def close(self):
        self.progress.finish()
        self._fh.close()

    def __len__(self):
        return self.total_size

    def __getattr__(self, name):
        return getattr(self._fh, name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


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
            co.auto_port()
            co.set_argument('--no-sandbox')
            co.set_argument('--disable-gpu')
            if HEADLESS_MODE:
                co.headless()
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

                    target_version = self.extract_version_from_text(urlparse(final_url).path) or self.extract_version_from_text(self.page.html)
                    existing_apk = self.get_existing_apk_path(folder, app_name, target_version)
                    if existing_apk:
                        logger.info(f"⏭️ APK já existe localmente para {app_name} v{target_version}: {existing_apk}")
                        return existing_apk

                    expected_asset_name = f"{app_name}_v{target_version}.apk" if target_version else ""
                    if self.release_asset_exists(app_config['repo'], target_version, expected_asset_name):
                        logger.info(f"⏭️ Release já publicada para {app_name} v{target_version}: {expected_asset_name}.")
                        if self.should_install_published_release(app_config.get('package_name', ''), target_version):
                            logger.info("📥 Baixando asset da release publicada para instalar/atualizar via adb...")
                            published_asset = await asyncio.to_thread(
                                self.download_published_release_asset,
                                app_config['repo'],
                                target_version,
                                expected_asset_name,
                            )
                            if published_asset:
                                return await self.wait_and_move_download(folder, app_name, published_asset)
                            logger.error("❌ Falha ao baixar asset já publicado para instalação via adb.")
                            return ""
                        logger.info("ℹ️ Download ignorado porque a release já está publicada e não há necessidade de instalar localmente.")
                        return ALREADY_PUBLISHED

                    downloaded_file = await asyncio.to_thread(self.download_apk_with_requests, final_url)
                    if downloaded_file:
                        return await self.wait_and_move_download(folder, app_name, downloaded_file)

                    logger.error("❌ Falha no download direto do APK.")
                    return ""
                
                logger.error("❌ Não foi possível encontrar o link final decodificado.")
        return ""
                        

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
                    total_size = int(response.headers.get("content-length") or 0)
                    downloaded_bytes = 0
                    progress = ConsoleProgressBar("Download APK", total_size)

                    if total_size:
                        logger.info(f"📦 Tamanho do download: {total_size / (1024 * 1024):.2f} MB")
                    else:
                        logger.info("📦 Tamanho do download não informado pelo servidor.")

                    with open(part_path, 'wb') as apk_file:
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            if not chunk:
                                continue

                            apk_file.write(chunk)
                            downloaded_bytes += len(chunk)
                            progress.update(downloaded_bytes)

                    progress.finish()

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

    async def wait_and_move_download(self, target_folder: str, app_name: str, downloaded_file: str = "") -> str:
        timeout = 300
        start_time = time.time()

        dest_dir = os.path.join(BASE_DIR, target_folder)
        os.makedirs(dest_dir, exist_ok=True)

        def move_file(latest_apk: str) -> str:
            version = self.extract_version_from_apk(latest_apk)
            if not version or version == "unknown":
                version = self.extract_version_from_filename(os.path.basename(latest_apk))
            version = re.sub(r'[^\w\.-]', '', version or "unknown")

            final_path = os.path.join(dest_dir, f"{app_name}_v{version}.apk")
            if os.path.exists(final_path):
                logger.info(f"⏭️ Versão {version} já existe.")
                os.remove(latest_apk)
                return final_path

            shutil.move(latest_apk, final_path)
            logger.info(f"🚀 Movido para: {final_path}")
            return final_path

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
        return ""

    def extract_version_from_apk(self, file_path: str) -> str:
        if not APK: return "unknown"
        try:
            apk = APK(file_path)
            return apk.version_name or apk.version_code or "unknown"
        except: return "unknown"

    def extract_version_from_filename(self, file_name: str) -> str:
        match = re.search(r'_v([0-9]+(?:\.[0-9]+)+)', file_name)
        if not match:
            return ""
        return match.group(1)

    def extract_version_from_text(self, text: str) -> str:
        candidate = os.path.basename(unquote(text or ""))
        for pattern in (r'v(\d+(?:\.\d+)+)', r'(\d+(?:\.\d+){2,})'):
            match = re.search(pattern, candidate, flags=re.IGNORECASE)
            if match:
                return match.group(1)
        return ""

    def get_existing_apk_path(self, target_folder: str, app_name: str, version: str) -> str:
        if not version:
            return ""
        apk_path = os.path.join(BASE_DIR, target_folder, f"{app_name}_v{version}.apk")
        return apk_path if os.path.exists(apk_path) else ""

    def extract_apk_metadata(self, file_path: str) -> Dict[str, str]:
        if not APK:
            return {}

        try:
            apk = APK(file_path)
            package_name = ""
            version_name = ""
            version_code = ""

            if hasattr(apk, "get_package"):
                package_name = apk.get_package() or ""
            if not package_name:
                package_name = getattr(apk, "packagename", "") or ""

            if hasattr(apk, "get_androidversion_name"):
                version_name = apk.get_androidversion_name() or ""
            if not version_name:
                version_name = getattr(apk, "version_name", "") or ""

            if hasattr(apk, "get_androidversion_code"):
                version_code = str(apk.get_androidversion_code() or "")
            if not version_code:
                version_code = str(getattr(apk, "version_code", "") or "")

            return {
                "package_name": package_name.strip(),
                "version_name": str(version_name).strip(),
                "version_code": str(version_code).strip(),
            }
        except Exception as exc:
            logger.warning(f"⚠️ Falha ao extrair metadados do APK: {exc}")
            return {}

    def get_installed_app_metadata(self, adb_path: str, serial: str, package_name: str) -> Dict[str, str]:
        try:
            result = subprocess.run(
                [adb_path, "-s", serial, "shell", "dumpsys", "package", package_name],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except Exception as exc:
            logger.warning(f"⚠️ Falha ao consultar versão instalada via adb: {exc}")
            return {}

        output = "\n".join(part for part in [(result.stdout or "").strip(), (result.stderr or "").strip()] if part)
        if result.returncode != 0 or "Unable to find package" in output or "Package [" in output and "was not found" in output:
            return {}

        version_name_match = re.search(r"versionName=([^\s]+)", output)
        version_code_match = re.search(r"versionCode=(\d+)", output)
        return {
            "package_name": package_name,
            "version_name": version_name_match.group(1).strip() if version_name_match else "",
            "version_code": version_code_match.group(1).strip() if version_code_match else "",
        }


    def load_github_token(self) -> str:
        env_token = os.getenv("GITHUB_TOKEN", "").strip()
        if env_token:
            return env_token

        token_file = os.path.join(BASE_DIR, "GITHUB_TOKEN.txt")
        if not os.path.exists(token_file):
            return ""
        with open(token_file, "r", encoding="utf-8") as fh:
            return fh.read().strip()

    def release_asset_exists(self, repo_name: str, version: str, asset_name: str) -> bool:
        if not version or not asset_name:
            return False

        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = self.load_github_token()
        if token:
            headers["Authorization"] = f"token {token}"

        try:
            release_url = f"https://api.github.com/repos/luascfl/{repo_name}/releases/tags/v{version}"
            response = requests.get(release_url, headers=headers, timeout=30)
            if response.status_code == 404:
                return False

            response.raise_for_status()
            release_data = response.json()
            existing_assets = {asset.get("name") for asset in release_data.get("assets", [])}
            return asset_name in existing_assets
        except Exception as exc:
            logger.warning(f"⚠️ Falha ao verificar asset já publicado: {exc}")
            return False

    def should_install_published_release(self, package_name: str, target_version: str) -> bool:
        if not package_name or not target_version:
            return False

        adb_path = self.get_adb_path()
        if not adb_path:
            return False

        devices = self.list_connected_adb_devices()
        if len(devices) != 1:
            return False

        serial = devices[0]
        installed_metadata = self.get_installed_app_metadata(adb_path, serial, package_name)
        if not installed_metadata:
            logger.info(f"ℹ️ App {package_name} não está instalado no dispositivo. Asset da release será baixado para instalação.")
            return True

        installed_version_name = installed_metadata.get("version_name", "")
        logger.info(
            f"📱 Versão instalada no dispositivo para {package_name}: {installed_version_name or '?'} | "
            f"versão da release: {target_version}"
        )
        if installed_version_name == target_version:
            logger.info("ℹ️ Dispositivo já está na mesma versão publicada. Sem download adicional.")
            return False

        logger.info("⬆️ Dispositivo está em versão diferente. Asset da release será baixado para atualização.")
        return True

    def download_published_release_asset(self, repo_name: str, version: str, asset_name: str) -> str:
        if not version or not asset_name:
            return ""

        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = self.load_github_token()
        if token:
            headers["Authorization"] = f"token {token}"

        try:
            release_url = f"https://api.github.com/repos/luascfl/{repo_name}/releases/tags/v{version}"
            response = requests.get(release_url, headers=headers, timeout=30)
            response.raise_for_status()
            release_data = response.json()

            asset = next((item for item in release_data.get("assets", []) if item.get("name") == asset_name), None)
            if not asset:
                logger.error(f"❌ Asset não encontrado na release publicada: {asset_name}")
                return ""

            download_url = asset.get("browser_download_url", "")
            if not download_url:
                logger.error(f"❌ browser_download_url ausente para o asset {asset_name}")
                return ""

            target_path = os.path.join(TEMP_DOWNLOAD_DIR, asset_name)
            part_path = f"{target_path}.part"
            if os.path.exists(part_path):
                os.remove(part_path)

            asset_size = int(asset.get("size") or 0)
            progress = ConsoleProgressBar("Download release APK", asset_size)
            logger.info(f"📦 Baixando asset já publicado: {asset_name} ({asset_size / (1024 * 1024):.2f} MB)")

            with requests.get(download_url, stream=True, timeout=(30, 1800)) as asset_response:
                asset_response.raise_for_status()
                downloaded_bytes = 0
                with open(part_path, "wb") as apk_file:
                    for chunk in asset_response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        apk_file.write(chunk)
                        downloaded_bytes += len(chunk)
                        progress.update(downloaded_bytes)

            progress.finish()
            os.replace(part_path, target_path)
            logger.info(f"✅ Asset da release baixado: {target_path}")
            return target_path
        except Exception as exc:
            logger.error(f"❌ Falha ao baixar asset já publicado: {exc}")
            return ""



    def ensure_release_asset(self, repo_name: str, apk_path: str) -> bool:
        token = self.load_github_token()
        if not token:
            logger.error("❌ GITHUB_TOKEN não encontrado (env ou GITHUB_TOKEN.txt).")
            return False

        file_name = os.path.basename(apk_path)
        version = self.extract_version_from_filename(file_name)
        if not version:
            logger.error(f"❌ Não foi possível extrair versão do arquivo: {file_name}")
            return False

        tag_name = f"v{version}"
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        try:
            release_url = f"https://api.github.com/repos/luascfl/{repo_name}/releases/tags/{tag_name}"
            response = requests.get(release_url, headers=headers, timeout=30)
            if response.status_code == 404:
                create_url = f"https://api.github.com/repos/luascfl/{repo_name}/releases"
                payload = {
                    "tag_name": tag_name,
                    "name": f"Release {tag_name}",
                    "body": f"Automated release of {tag_name}",
                    "draft": False,
                    "prerelease": False,
                    "target_commitish": "main",
                }
                response = requests.post(create_url, headers=headers, json=payload, timeout=30)
                response.raise_for_status()
                release_data = response.json()
                logger.info(f"✅ Release criada: {repo_name}/{tag_name}")
            else:
                response.raise_for_status()
                release_data = response.json()
                logger.info(f"✅ Release já existe: {repo_name}/{tag_name}")

            existing_assets = {asset.get("name") for asset in release_data.get("assets", [])}
            if file_name in existing_assets:
                logger.info(f"✅ Asset já existe na release: {file_name}")
                return True

            upload_url = release_data.get("upload_url", "").split("{")[0]
            file_size_bytes = os.path.getsize(apk_path)
            file_size_mb = file_size_bytes / (1024 * 1024)
            logger.info(f"📤 Enviando asset para a release: {file_name} ({file_size_mb:.2f} MB). Pode levar alguns minutos.")

            if not upload_url:
                logger.error("❌ upload_url ausente na resposta da release.")
                return False

            upload_headers = headers.copy()
            upload_headers["Content-Type"] = "application/vnd.android.package-archive"
            upload_headers["Content-Length"] = str(file_size_bytes)

            with ProgressFile(apk_path, f"Upload release {file_name}", file_size_bytes) as apk_file:
                upload_response = requests.post(
                    upload_url,
                    params={"name": file_name},
                    headers=upload_headers,
                    data=apk_file,
                    timeout=(30, 1800),
                )

            if upload_response.status_code not in (200, 201):
                logger.error(f"❌ Falha ao enviar asset: {upload_response.status_code} - {upload_response.text[:300]}")
                return False

            browser_url = upload_response.json().get("browser_download_url")
            logger.info(f"✅ Asset publicado: {browser_url}")
            return True
        except Exception as exc:
            logger.error(f"❌ Falha ao publicar release: {exc}")
            return False

    def cleanup_local_apks_for_release_only(self, target_folder: str):
        folder_path = os.path.join(BASE_DIR, target_folder)
        os.makedirs(folder_path, exist_ok=True)

        gitignore_path = os.path.join(folder_path, ".gitignore")
        if os.path.exists(gitignore_path):
            with open(gitignore_path, "r", encoding="utf-8") as fh:
                content = fh.read()
        else:
            content = ""

        if "*.apk" not in {line.strip() for line in content.splitlines()}:
            with open(gitignore_path, "a", encoding="utf-8") as fh:
                if content and not content.endswith("\n"):
                    fh.write("\n")
                fh.write("*.apk\n")
            logger.info(f"✅ .gitignore atualizado em {target_folder} com *.apk")

        removed = []
        for apk_file in glob.glob(os.path.join(folder_path, "*.apk")):
            os.remove(apk_file)
            removed.append(os.path.basename(apk_file))

        if removed:
            logger.info(f"🧹 APKs removidos do repo {target_folder}: {', '.join(removed)}")
        else:
            logger.info(f"ℹ️ Nenhum APK local para remover em {target_folder}")


    def get_adb_path(self) -> str:
        return shutil.which("adb") or ""

    def list_connected_adb_devices(self) -> List[str]:
        adb_path = self.get_adb_path()
        if not adb_path:
            logger.info("ℹ️ adb não encontrado no PATH. Instalação no dispositivo será ignorada.")
            return []

        try:
            result = subprocess.run(
                [adb_path, "devices"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except Exception as exc:
            logger.warning(f"⚠️ Falha ao consultar dispositivos adb: {exc}")
            return []

        if result.returncode != 0:
            logger.warning(f"⚠️ adb devices falhou: {(result.stderr or result.stdout).strip()}")
            return []

        devices: List[str] = []
        blocked: List[str] = []
        for raw_line in result.stdout.splitlines()[1:]:
            line = raw_line.strip()
            if not line:
                continue

            parts = line.split()
            if len(parts) < 2:
                continue

            serial, state = parts[0], parts[1]
            if state == "device":
                devices.append(serial)
            else:
                blocked.append(f"{serial} ({state})")

        if blocked:
            logger.warning(f"⚠️ Dispositivos adb indisponíveis: {', '.join(blocked)}")

        return devices

    def install_apk_if_adb_available(self, apk_path: str) -> bool:
        adb_path = self.get_adb_path()
        if not adb_path:
            logger.info("ℹ️ adb não encontrado no PATH. Instalação automática ignorada.")
            return False

        devices = self.list_connected_adb_devices()
        if not devices:
            logger.info("ℹ️ Nenhum dispositivo adb conectado em estado 'device'.")
            return False

        if len(devices) > 1:
            logger.warning(f"⚠️ Mais de um dispositivo adb conectado ({', '.join(devices)}). Instalação automática ignorada.")
            return False

        serial = devices[0]
        apk_metadata = self.extract_apk_metadata(apk_path)
        package_name = apk_metadata.get("package_name", "")
        apk_version_name = apk_metadata.get("version_name", "")
        apk_version_code = apk_metadata.get("version_code", "")

        if package_name:
            installed_metadata = self.get_installed_app_metadata(adb_path, serial, package_name)
            if installed_metadata:
                installed_version_name = installed_metadata.get("version_name", "")
                installed_version_code = installed_metadata.get("version_code", "")
                logger.info(
                    f"📱 Versão instalada no dispositivo para {package_name}: "
                    f"name={installed_version_name or '?'} code={installed_version_code or '?'}"
                )
                logger.info(
                    f"📦 Versão do APK baixado: "
                    f"name={apk_version_name or '?'} code={apk_version_code or '?'}"
                )

                same_version_name = bool(installed_version_name and apk_version_name and installed_version_name == apk_version_name)
                same_version_code = bool(installed_version_code and apk_version_code and installed_version_code == apk_version_code)
                if same_version_name and (same_version_code or not installed_version_code or not apk_version_code):
                    logger.info("ℹ️ O mesmo build já está instalado no dispositivo. Instalação ignorada.")
                    return False

                logger.info("⬆️ Versão diferente detectada no dispositivo. Atualização será aplicada.")
            else:
                logger.info(f"ℹ️ App {package_name} não está instalado no dispositivo. Instalação será feita.")
        else:
            logger.warning("⚠️ Não foi possível identificar o pacote do APK. Tentando instalar mesmo assim.")

        logger.info(f"📲 Instalando APK via adb no dispositivo {serial}...")

        try:
            result = subprocess.run(
                [adb_path, "-s", serial, "install", "-r", apk_path],
                capture_output=True,
                text=True,
                timeout=900,
                check=False,
            )
        except Exception as exc:
            logger.warning(f"⚠️ Falha ao instalar APK via adb: {exc}")
            return False

        output = (result.stdout or "").strip()
        error_output = (result.stderr or "").strip()
        if result.returncode == 0:
            if output:
                logger.info(f"✅ Instalação adb concluída: {output}")
            else:
                logger.info("✅ Instalação adb concluída.")
            return True

        logger.warning(f"⚠️ Instalação adb falhou: {(error_output or output or f'código {result.returncode}')}")
        return False

    async def cleanup(self):
        if self.page: self.page.quit()
        if os.path.exists(TEMP_DOWNLOAD_DIR):
            shutil.rmtree(TEMP_DOWNLOAD_DIR, ignore_errors=True)

async def main():
    print("="*50)
    print(f"🚀 APK BUILDER (Native Download Mode)")
    print("="*50)

    apps = [
        {"name": "Endel", "folder": "Endel", "repo": "endel", "package_name": "com.endel.endel", "url": "https://liteapks.com/endel.html"},
        {"name": "CamScanner", "folder": "CamScanner", "repo": "CamScanner", "package_name": "com.intsig.camscanner", "url": "https://liteapks.com/camscanner.html"}
    ]

    scraper = APKScraper()
    should_push = False
    try:
        await scraper.init_browser()
        for app in apps:
            print(f"\n📱 {app['name']}...")
            final_apk_path = await scraper.process_liteapks(app)
            if final_apk_path == ALREADY_PUBLISHED:
                logger.info(f"ℹ️ {app['name']} já está publicado na release. Nada para baixar.")
                continue

            if not final_apk_path:
                logger.error(f"❌ Falha no download de {app['name']}")
                continue

            scraper.install_apk_if_adb_available(final_apk_path)

            if not scraper.ensure_release_asset(app['repo'], final_apk_path):
                logger.error(f"❌ Falha ao publicar release de {app['name']}")
                continue

            scraper.cleanup_local_apks_for_release_only(app['folder'])
            should_push = True
    finally:
        await scraper.cleanup()
        print("\n🏁 Finalizado.")
        if should_push:
            print("\n🚀 Iniciando Push para GitHub (Subfolders + Releases)...")
            try:
                subprocess.run(["./create_and_push_repo.sh", "push"], check=True)
                print("✅ Push e Releases concluídos!")
                print("\n🔗 LINKS DAS ÚLTIMAS RELEASES:")
            except subprocess.CalledProcessError as exc:
                logger.error(f"❌ Falha no push/repos: {exc}")
        else:
            logger.info("ℹ️ Nada novo para publicar. Push ignorado.")

if __name__ == "__main__":
    asyncio.run(main())
