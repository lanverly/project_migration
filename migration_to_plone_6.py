"""
Migração de Notícias: Trensurb (Plone 4) → Plone 6
=====================================================

Autor: Byron Lanverly

Faz scraping de TODAS as notícias de:
  https://www.gov.br/trensurb/pt-br/assuntos/noticias
e insere no Plone 6 via API REST com autenticação JWT.

Utilizada a API oficila do Plone: https://6.docs.plone.org/plone.api/index.html

Dependências:
    pip install requests beautifulsoup4 lxml

Uso:
    1. Preencha o bloco CONFIG abaixo
    2. Habilitar venv (se for o caso) $ source .venv/bin/activate
    2. Execute: $ python trensurb_migrar_noticias.py
    3. Acompanhe o progresso em: migracao.log
"""

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup, NavigableString
import time
import logging
import base64
import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime
from urllib.parse import urljoin
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import sys

# Globais para controle via Módulo
CONFIG = {}
STOP_SIGNAL = False
log = logging.getLogger(__name__)

def setup_log(level=logging.INFO):
    """Configura log padrão para execução CLI."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("migracao.log", encoding="utf-8"),
        ],
    )
    return logging.getLogger(__name__)

def load_progress(path: str) -> set:
    if os.path.exists(path) and os.path.getsize(path) > 0:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                log.warning(f"Arquivo de progresso corrompido, reiniciando.")
                return set()
            return set(data)
        except (json.JSONDecodeError, ValueError) as e:
            log.warning(f"Erro ao ler progresso ({e}), reiniciando.")
            return set()
    return set()

def save_progress(path: str, done: set):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(sorted(done), f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)

@dataclass
class NewsItem:
    title:          str
    body:           str
    url:            str
    summary:        str = ""
    date:           Optional[str] = None
    image_url:      Optional[str] = None
    image_caption:  str = ""
    category:       str = ""
    tags:           list[str] = field(default_factory=list)

class PloneClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.api_url = self._build_api_url(self.base_url)
        auth_value = token if token.startswith("Bearer ") else f"Bearer {token}"
        self.session = requests.Session()
        self.session.headers.update({
            "Accept":        "application/json",
            "Content-Type":  "application/json",
            "Authorization": auth_value,
        })
        self.session.verify = False
        self._verify()

    @staticmethod
    def _build_api_url(base_url: str) -> str:
        """Insere ++api++ no caminho para acessar a REST API do backend Plone.
        Ex: https://host/pasta/pt-br → https://host/pasta/++api++/pt-br
        """
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(base_url)
        parts = parsed.path.rstrip("/").split("/")
        if len(parts) >= 2 and "++api++" not in parts:
            parts.insert(2, "++api++")
        new_path = "/".join(parts)
        return urlunparse(parsed._replace(path=new_path))

    def _verify(self):
        """Valida o token fazendo um GET na raiz do site Plone."""
        resp = self.session.get(self.api_url, timeout=(5, 10))
        if resp.status_code == 401:
            raise RuntimeError("Token Bearer inválido ou expirado.")
        resp.raise_for_status()
        data = resp.json()
        site_type = data.get("@type", "?")
        site_title = data.get("title", "?")
        log.info(f"✅ Conectado ao Plone 6: '{site_title}' (tipo: {site_type})")

    def ensure_folder_exists(self, folder_path: str):
        """Verifica se a estrutura de pastas existe no alvo e cria as que faltarem recursivamente."""
        if not folder_path: return
        parts = [p for p in folder_path.strip("/").split("/") if p]
        current_path = ""
        for i, part in enumerate(parts):
            is_last = (i == len(parts) - 1)
            next_path = f"{current_path}/{part}" if current_path else f"/{part}"
            url_to_check = f"{self.api_url}{next_path}"
            
            resp = self.session.get(url_to_check, timeout=(5, 15))
            if resp.status_code == 404:
                parent_url = f"{self.api_url}{current_path}" if current_path else self.api_url
                title = part.replace("-", " ").title()
                
                # Se for o último e migrate_as_self, cria como o tipo final (ex: Document)
                # Senão, cria como Folder estrutural
                p_type = "Folder"
                if is_last and CONFIG.get("migrate_as_self"):
                    p_type = CONFIG.get("portal_type", "Document")

                payload = {
                    "@type": p_type,
                    "id": part,
                    "title": title
                }
                # category apenas para News Items (Páginas de listagem são Document)
                if p_type == "News Item":
                    payload["category"] = "Institucional"

                # Se for o tipo final, já inicializa com blocos vazios para evitar erro no Volto
                if p_type == "Document" or is_last:
                    payload["blocks"] = {"abc": {"@type": "slate", "value": [{"type": "p", "children": [{"text": ""}]}], "plaintext": ""}}
                    payload["blocks_layout"] = {"items": ["abc"]}

                log.info(f"   📁 Auto-criando {'pasta' if p_type == 'Folder' else 'página'} base: {next_path} ({p_type})...")
                create_resp = self.session.post(parent_url, json=payload, timeout=(5, 30))
                
                if create_resp.ok:
                    res_json = create_resp.json()
                    self._publish(res_json.get("@id", ""))
                elif create_resp.status_code == 400 and "already in use" in create_resp.text.lower():
                    log.info(f"   ℹ️  Elemento já existe em {next_path} (ID em uso).")
                else:
                    log.warning(f"   ⚠️  Erro ao auto-criar {next_path}. Detalhe: {create_resp.text}")
            
            current_path = next_path

    def get_content_url(self, folder_path: str, title: str) -> Optional[str]:
        """Retorna o @id (URL) se já existe conteúdo com mesmo título na pasta."""
        try:
            resp = self.session.get(
                f"{self.api_url}/@search",
                params={"Title": title, "path.query": folder_path, "path.depth": "1"},
                timeout=(5, 15),
            )
            if resp.ok and "application/json" in resp.headers.get("Content-Type", ""):
                for item in resp.json().get("items", []):
                    if item.get("title", "").strip().lower() == title.strip().lower():
                        return item.get("@id")
        except Exception as e:
            log.warning(f"   ⚠️  Erro ao buscar duplicata do conteúdo: {e}")
        return None

    # Cache para listagem de pastas
    _folder_cache = {}

    def get_file_url(self, folder_path: str, filename: str) -> Optional[str]:
        """Retorna o @id (URL) se o Arquivo (File) já existir na pasta destino."""
        # 1. Busca por index via @search (original)
        try:
            resp = self.session.get(
                f"{self.api_url}/@search",
                params={"portal_type": "File", "Title": filename, "path.query": folder_path, "path.depth": "1"},
                timeout=(5, 10),
            )
            if resp.ok and "application/json" in resp.headers.get("Content-Type", ""):
                for item in resp.json().get("items", []):
                    if item.get("title", "").strip() == filename.strip():
                        return item.get("@id")
        except Exception:
            pass

        # 2. Se falhar, lista os itens da pasta (com cache)
        if folder_path not in self._folder_cache:
            try:
                folder_url = f"{self.api_url}{folder_path}" if folder_path else self.api_url
                # b_size=1000 para garantir que pegue todos os arquivos da pasta
                resp = self.session.get(folder_url, params={"b_size": 1000}, timeout=(5, 15))
                if resp.ok:
                    self._folder_cache[folder_path] = resp.json().get("items", [])
                else:
                    self._folder_cache[folder_path] = []
            except Exception:
                self._folder_cache[folder_path] = []
        
        # Procura no cache
        fn_clean = filename.strip().lower()
        for item in self._folder_cache[folder_path]:
            itm_title = item.get("title", "").strip().lower()
            itm_id = item.get("id", "").strip().lower()
            if fn_clean == itm_title or fn_clean == itm_id or fn_clean in itm_title:
                return item.get("@id")

        return None

    def create_empty_news(self, folder_path: str, news: NewsItem) -> Optional[dict]:
        """Cria base do News Item/Document no Plone 6 (Volto) vazio (pre-criação)."""
        portal_type = CONFIG.get("portal_type", "News Item")
        
        title = news.title.strip() if news.title else ""
        if not title:
            title = "Página sem Título - Migração"
            log.warning(f"   ⚠️  Título não encontrado. Usando fallback '{title}'.")
            news.title = title
            
        payload = {
            "@type": portal_type,
            "title": title,
        }
        # Fallback obrigatório: O Plone do Serpro exige Categoria
        cat = news.category.strip() if news.category else "Institucional"
        payload["category"] = cat
        log.info(f"   📂 Categoria: {cat}")
        
        if news.tags:
            payload["subjects"] = news.tags
            log.info(f"   🏷️  Tags: {', '.join(news.tags)}")
        if news.summary:
            payload["description"] = news.summary[:250]
        if news.date:
            payload["effective"] = news.date
        if news.image_caption and portal_type == "News Item":
            payload["image_caption"] = news.image_caption

        try:
            target_url = f"{self.api_url}{folder_path}" if folder_path else self.api_url
            resp = self.session.post(target_url, json=payload, timeout=(5, 30))
            if resp.status_code == 201:
                result = resp.json()
                log.info(f"   ✅ Base Vazia Criada → {title[:70]}")
                return result
            else:
                log.error(f"   ❌ HTTP {resp.status_code} ao criar base: {resp.text[:300]}")
                return None
        except Exception as e:
            log.error(f"   ❌ Erro ao criar base: {e}")
            return None

    def _api_id(self, url: str) -> str:
        """Converte a URL @id do Frontend para a URL da REST API."""
        if url and "++api++" not in url:
            return url.replace("/pt-br", "/++api++/pt-br", 1)
        return url

    def patch_news_blocks(self, item_url: str, news: NewsItem) -> bool:
        """Injeta a estrutura de blocos e metadados via PATCH em item existente."""
        import uuid as _uuid
        content_blocks, content_layout = self._html_to_volto_blocks(news.body)
        portal_type = CONFIG.get("portal_type", "News Item")

        title_uid  = str(_uuid.uuid4())
        desc_uid   = str(_uuid.uuid4())
        
        # Blocos base para qualquer tipo
        blocks = {
            title_uid: {"@type": "title"},
            desc_uid:  {"@type": "description"},
        }
        layout_items = [title_uid, desc_uid]

        # Blocos exclusivos para News Item
        if portal_type == "News Item":
            social_uid = str(_uuid.uuid4())
            audio_uid  = str(_uuid.uuid4())
            lead_uid   = str(_uuid.uuid4())
            
            blocks.update({
                social_uid: {"@type": "dateSocialShareBlock"},
                audio_uid: {"@type": "textToSpeech"},
                lead_uid:  {"@type": "leadimage"},
            })
            layout_items += [social_uid, audio_uid, lead_uid]

        # Adiciona o conteúdo real (Slate/Tabelas/Imagens)
        blocks.update(content_blocks)
        layout_items += content_layout["items"]

        # Payload consolidado com Hard Reset de metadados críticos
        payload = {
            "title": news.title, # Sincroniza título raiz com o bloco title
            "blocks": blocks,
            "blocks_layout": {"items": layout_items},
            "description": news.summary.strip() if news.summary else "",
            "subjects": news.tags if news.tags else []
        }

        # category apenas para News Items
        if portal_type == "News Item":
            payload["category"] = news.category.strip() if news.category else "Institucional"

        try:
            target_url = self._api_id(item_url)

            # ── Desbloqueio automático (caso a página esteja aberta no editor) ──
            lock_resp = self.session.delete(f"{target_url}/@lock", timeout=(5, 15))
            if lock_resp.status_code == 200:
                log.info("   🔓 Lock removido com sucesso.")
            elif lock_resp.status_code == 403:
                log.warning("   ⚠️  Não foi possível remover o lock (403). Tentando PATCH assim mesmo...")

            log.info(f"   📝 Injetando blocos no Plone (PATCH)...")
            resp = self.session.patch(target_url, json=payload, timeout=(5, 60))
            if resp.ok:
                log.info(f"   🎯 Corpo atualizado com sucesso e publicado!")
                self._publish(item_url)
                return True
            else:
                log.error(f"   ❌ HTTP {resp.status_code} no PATCH: {resp.text[:300]}")
                return False
        except Exception as e:
            log.error(f"   ❌ Erro de conexão no PATCH: {e}")
            return False

    @staticmethod
    def _html_to_volto_blocks(html: str) -> tuple[dict, dict]:
        """Converte HTML em blocos Volto/Slate para o campo blocks.

        Cada parágrafo, cabeçalho, lista e imagem vira um bloco separado.
        Retorna (blocks_dict, blocks_layout_dict).
        """
        import uuid
        soup = BeautifulSoup(html, "lxml")
        blocks = {}
        order = []

        BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6",
                      "ul", "ol", "blockquote", "table", "pre", "hr"}

        HEADING_MAP = {
            "h1": "h2", "h2": "h2", "h3": "h3",
            "h4": "h4", "h5": "h5", "h6": "h6",
        }

        def _parse_inline_elements(element):
            """Analisa os elementos filhos de um bloco e retorna a lista de nós Slate."""
            nodes = []
            for child in element.children:
                if isinstance(child, NavigableString):
                    text = str(child).replace('\n', ' ')
                    if text:
                        nodes.append({"text": text})
                elif getattr(child, "name", None):
                    tag = child.name
                    
                    if tag in ["strong", "b"]:
                        nodes.append({"type": "strong", "children": _parse_inline_elements(child)})
                    elif tag in ["em", "i"]:
                        nodes.append({"type": "i", "children": _parse_inline_elements(child)})
                    elif tag == "u":
                        nodes.append({"type": "u", "children": _parse_inline_elements(child)})
                    elif tag in ["s", "strike"]:
                        nodes.append({"type": "del", "children": _parse_inline_elements(child)})
                    elif tag == "a":
                        href = child.get("href")
                        if href:
                            nodes.append({
                                "type": "link",
                                "data": {"url": href},
                                "children": _parse_inline_elements(child)
                            })
                        else:
                            # Link sem href: trata como texto normal ou apenas os filhos
                            nodes.extend(_parse_inline_elements(child))
                    elif tag in ["br"]:
                        nodes.append({"text": "\n"})
                    else:
                        nodes.extend(_parse_inline_elements(child))
            
            if not nodes:
                nodes = [{"text": ""}]
            return nodes

        def _extract_blocks(container):
            """Percorre recursivamente o container extraindo blocos."""
            for el in container.children:
                if isinstance(el, NavigableString):
                    text = el.strip()
                    if text:
                        uid = str(uuid.uuid4())
                        blocks[uid] = {
                            "@type": "slate",
                            "value": [{"type": "p", "children": [{"text": text}]}],
                            "plaintext": text,
                        }
                        order.append(uid)
                    continue

                tag_name = getattr(el, "name", None)
                if not tag_name:
                    continue

                if tag_name in ("div", "section", "article", "main"):
                    _extract_blocks(el)
                    continue

                if tag_name == "img" and el.get("src"):
                    uid = str(uuid.uuid4())
                    blocks[uid] = {
                        "@type": "image",
                        "url": el["src"],
                        "alt": el.get("alt", ""),
                    }
                    order.append(uid)
                    continue

                if tag_name == "figure":
                    img = el.find("img")
                    if img and img.get("src"):
                        uid = str(uuid.uuid4())
                        caption = ""
                        figcap = el.find("figcaption")
                        if figcap:
                            caption = figcap.get_text(strip=True)
                        blocks[uid] = {
                            "@type": "image",
                            "url": img["src"],
                            "alt": img.get("alt", caption),
                        }
                        order.append(uid)
                    continue

                if tag_name == "table":
                    uid = str(uuid.uuid4())

                    # ── Detecta thead, tbody e tfoot ───────────────────────
                    thead = el.find("thead")
                    thead_tr_ids = set(id(tr) for tr in (thead.find_all("tr") if thead else []))
                    all_trs = el.find_all("tr")

                    if not all_trs:
                        continue

                    # ── Calcula largura máxima (com colspan) ───────────────
                    max_cols = 0
                    for tr in all_trs:
                        c = sum(int(cell.get("colspan", 1)) for cell in tr.find_all(["th", "td"]))
                        max_cols = max(max_cols, c)

                    if max_cols == 0:
                        continue

                    # ── Grade virtual para rowspan/colspan ─────────────────
                    n_rows = len(all_trs)
                    grid = [[None] * max_cols for _ in range(n_rows)]

                    for r_idx, tr in enumerate(all_trs):
                        is_header_row = id(tr) in thead_tr_ids
                        c_idx = 0
                        for cell in tr.find_all(["th", "td"]):
                            # Pula colunas já preenchidas por rowspan anterior
                            while c_idx < max_cols and grid[r_idx][c_idx] is not None:
                                c_idx += 1
                            if c_idx >= max_cols:
                                break

                            cell_type   = "header" if (is_header_row or cell.name == "th") else "data"
                            cell_inline = _parse_inline_elements(cell)
                            colspan     = max(1, int(cell.get("colspan", 1)))
                            rowspan     = max(1, int(cell.get("rowspan", 1)))
                            colspan     = min(colspan, max_cols - c_idx)

                            # Célula principal
                            main_cell = {
                                "key":   str(uuid.uuid4()),
                                "type":  cell_type,
                                "value": [{"type": "p", "children": cell_inline or [{"text": ""}]}],
                            }

                            # Preenche a área rowspan × colspan na grade
                            for dr in range(rowspan):
                                if r_idx + dr >= n_rows:
                                    break
                                for dc in range(colspan):
                                    if c_idx + dc >= max_cols:
                                        break
                                    if dr == 0 and dc == 0:
                                        grid[r_idx][c_idx] = main_cell
                                    else:
                                        # Célula de preenchimento vazia
                                        grid[r_idx + dr][c_idx + dc] = {
                                            "key":   str(uuid.uuid4()),
                                            "type":  cell_type,
                                            "value": [{"type": "p", "children": [{"text": ""}]}],
                                        }
                            c_idx += colspan

                    # ── Constrói rows_data a partir da grade ───────────────
                    rows_data = []
                    for row in grid:
                        cells_data = []
                        for c in row:
                            if c is not None:
                                cells_data.append(c)
                            else:
                                # Preenche buracos de tabelas HTML irregulares
                                cells_data.append({
                                    "key": str(uuid.uuid4()),
                                    "type": "data",
                                    "value": [{"type": "p", "children": [{"text": ""}]}]
                                })
                        
                        if cells_data:
                            rows_data.append({"key": str(uuid.uuid4()), "cells": cells_data})

                    if not rows_data:
                        continue

                    blocks[uid] = {
                        "@type": "slateTable",
                        "table": {
                            "basic":    False,
                            "celled":   True,
                            "compact":  False,
                            "fixed":    True,
                            "inverted": False,
                            "striped":  False,
                            "rows":     rows_data,
                        }
                    }
                    order.append(uid)
                    continue

                if tag_name == "hr":
                    uid = str(uuid.uuid4())
                    blocks[uid] = {"@type": "slate", "value": [{"type": "p", "children": [{"text": "---"}]}], "plaintext": "---"}
                    order.append(uid)
                    continue

                uid = str(uuid.uuid4())
                slate_type = HEADING_MAP.get(tag_name, "p")
                
                if tag_name in ("ul", "ol"):
                    list_type = "ul" if tag_name == "ul" else "ol"
                    list_items = []
                    for li in el.find_all("li", recursive=False):
                        li_children = _parse_inline_elements(li)
                        list_items.append({"type": "li", "children": li_children})
                    if not list_items:
                        continue
                    blocks[uid] = {
                        "@type": "slate",
                        "value": [{"type": list_type, "children": list_items}],
                        "plaintext": el.get_text(strip=True),
                    }
                    order.append(uid)
                    continue

                children_nodes = _parse_inline_elements(el)
                plaintext = el.get_text(strip=True)
                
                if not plaintext and all(not n.get("text", "").strip() and "type" not in n for n in children_nodes):
                    continue

                blocks[uid] = {
                    "@type": "slate",
                    "value": [{"type": slate_type, "children": children_nodes}],
                    "plaintext": plaintext,
                }
                order.append(uid)

        content = soup.find("body") or soup
        _extract_blocks(content)

        if not order:
            uid = str(uuid.uuid4())
            blocks[uid] = {"@type": "slate", "value": [{"type": "p", "children": [{"text": ""}]}]}
            order.append(uid)

        return blocks, {"items": order}

    def _publish(self, content_url: str):
        """Realiza a transição de workflow para 'publish'. Silencioso em caso de já publicado."""
        try:
            wf_url = self._api_id(content_url)
            resp = self.session.post(
                f"{wf_url}/@workflow/publish",
                json={},
                timeout=(5, 15),
            )
            if resp.ok:
                log.info("   📢 Item publicado.")
            elif resp.status_code == 400:
                pass # Silencioso: Provavelmente já está publicado ou estado incompatível
            else:
                log.warning(f"   ⚠️  Workflow publish retornou {resp.status_code}")
        except Exception as e:
            log.warning(f"   ⚠️  Erro ao publicar: {e}")

    def upload_image(self, news_plone_url: str, image_url: str, caption: str = ""):
        """Faz upload da imagem de destaque (lead image) para a notícia já criada."""
        try:
            img = requests.get(image_url, timeout=(5, 30))
            img.raise_for_status()
            ct = img.headers.get("Content-Type", "image/jpeg")
            if not ct.startswith("image/"):
                log.warning(f"   ⚠️  URL não é imagem (Content-Type: {ct}). Ignorando.")
                return
            fn = image_url.split("/")[-1].split("?")[0] or "imagem.jpg"
            fn = fn[:100]
            payload = {"image": {
                "data":         base64.b64encode(img.content).decode(),
                "encoding":     "base64",
                "content-type": ct,
                "filename":     fn,
            }}
            if caption:
                payload["image_caption"] = caption
            patch_url = self._api_id(news_plone_url)
            resp = self.session.patch(patch_url, json=payload, timeout=(5, 60))
            resp.raise_for_status()
            log.info(f"   🖼️  Lead image: {fn}" + (f" | Legenda: {caption[:60]}" if caption else ""))
        except Exception as e:
            log.warning(f"   ⚠️  Imagem não enviada: {e}")

    def upload_file_attachment(self, folder_path: str, source_url: str, scraper_session) -> str:
        """Faz download do arquivo da origem e cria um objeto File no Plone 6."""
        try:
            resp = scraper_session.get(source_url, stream=True, timeout=(5, 60))
            if resp.status_code != 200:
                log.warning(f"   ⚠️  Arquivo não encontrado na origem: {source_url}")
                return source_url
                
            ct = resp.headers.get("Content-Type", "application/octet-stream")
            fn = source_url.split("/")[-1].split("?")[0]
            if not fn or fn.lower() in ("download", "view", "at_download", "file", ""):
                import hashlib
                h = hashlib.md5(source_url.encode()).hexdigest()[:8]
                fn = f"arquivo_{h}.pdf" 

            if "pdf" in ct and not fn.endswith(".pdf"): fn += ".pdf"
            
            existing_file_url = self.get_file_url(folder_path, fn)
            if existing_file_url:
                resp.close()
                log.info(f"   📎 Arquivo já existe, reaproveitado: {fn}")
                return existing_file_url
            
            payload = {
                "@type": "File",
                "title": fn,
                "file": {
                    "data": base64.b64encode(resp.content).decode(),
                    "encoding": "base64",
                    "content-type": ct,
                    "filename": fn
                }
            }
            
            target_url = f"{self.api_url}{folder_path}" if folder_path else self.api_url
            post_resp = self.session.post(target_url, json=payload, timeout=(5, 60))
            post_resp.raise_for_status()
            res = post_resp.json()
            
            self._publish(res["@id"])
            log.info(f"   📎 Arquivo migrado: {fn}")
            return res.get("@id", source_url)
        except Exception as e:
            log.warning(f"   ⚠️  Erro ao migrar arquivo {source_url}: {e}")
            return source_url


class TrensurbScraper:

    def __init__(self, cfg: dict):
        self.cfg  = cfg
        self.base = cfg["source_base"]
        self.sess = requests.Session()
        self.sess.headers.update({
            "User-Agent":      "Mozilla/5.0 (compatible; MigradorPlone/1.0)",
            "Accept-Language": "pt-BR,pt;q=0.9",
        })
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.sess.mount("https://", adapter)
        self.sess.mount("http://", adapter)

    def _soup(self, url: str) -> Optional[BeautifulSoup]:
        try:
            r = self.sess.get(url, timeout=(5, 20))
            r.raise_for_status()
            return BeautifulSoup(r.text, "lxml")
        except requests.RequestException as e:
            log.error(f"Erro ao acessar {url}: {e}")
            return None

    def _abs(self, href: str, context: str) -> str:
        if href.startswith("http"):
            return href
        if href.startswith("/"):
            return self.base + href
        return urljoin(context, href)

    def _parse_date(self, raw: str) -> Optional[str]:
        """Converte datas em vários formatos para ISO 8601."""
        raw = raw.strip()

        raw = re.sub(r'^(Publicado|Atualizado|Criado)\s*em\s*', '', raw, flags=re.IGNORECASE).strip()

        raw = re.sub(r'(\d{2})h(\d{2})', r'\1:\2', raw)

        for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw, fmt).strftime("%Y-%m-%dT%H:%M:%S-03:00")
            except ValueError:
                continue
        for fmt, length in (("%d/%m/%Y %H:%M", 16), ("%d/%m/%Y", 10), ("%Y-%m-%d", 10)):
            try:
                return datetime.strptime(raw[:length], fmt).strftime("%Y-%m-%dT%H:%M:%S-03:00")
            except ValueError:
                continue
        log.warning(f"   ⚠️  Data não reconhecida: '{raw}'")
        return None

    def get_links(self) -> list[str]:
        links   = []
        url     = self.cfg["source_start"]
        max_n   = self.cfg.get("max_news", 0)
        seen    = set()

        while url:
            log.info(f"📋 Listagem/Página: {url}")
            soup = self._soup(url)
            if not soup:
                break

            items = soup.select("li h2 a")
            if not items and url == self.cfg["source_start"]:
                log.info("   Nenhuma lista de itens padrão encontrada. Assumindo como página única.")
                links.append(url)
                break

            for a in items:
                href = a.get("href", "")
                if not href:
                    continue
                full = self._abs(href, url)
                if full not in seen:
                    seen.add(full)
                    links.append(full)
                if max_n and len(links) >= max_n:
                    break

            if max_n and len(links) >= max_n:
                break

            if self.cfg.get("all_pages"):
                nxt = (
                    soup.select_one("a[title='Próximo']")
                    or soup.select_one("a[title='Next']")
                    or soup.select_one("a.proximo")
                    or soup.select_one("a.next")
                )
                if nxt and nxt.get("href"):
                    url = self._abs(nxt["href"], url)
                    time.sleep(self.cfg["delay"])
                else:
                    break
            else:
                break

        log.info(f"   → {len(links)} links coletados.")
        return links

    def scrape(self, url: str) -> Optional[NewsItem]:
        soup = self._soup(url)
        if not soup:
            return None

        # Título
        title_tag = (
            soup.select_one("h1.documentFirstHeading")
            or soup.select_one("h1.page-header")
            or soup.select_one("#content h1")
            or soup.select_one("h1")
        )
        if not title_tag:
            log.warning(f"Sem título: {url}")
            return None
        title = title_tag.get_text(strip=True)

        # Resumo
        summary = ""
        for sel in ["div.documentDescription", "div.chamada", "p.description"]:
            s = soup.select_one(sel)
            if s:
                summary = s.get_text(strip=True)
                break

        body_tag = (
            soup.select_one("div#content-core")
            or soup.select_one("div.item-description")
            or soup.select_one("div.rich-text")
            or soup.select_one("article")
        )
        if body_tag:
            for tag in body_tag.select("script, style, nav, header, footer"):
                tag.decompose()
        body_html = str(body_tag) if body_tag else "<p>(corpo não encontrado)</p>"

        # Data
        date = None
        for sel in ["span.documentPublished", "span.documentModified", "time", ".data"]:
            d = soup.select_one(sel)
            if d:
                raw = d.get("datetime") or d.get_text(strip=True)
                date = self._parse_date(raw)
                if date:
                    break

        image_url     = None
        image_caption = ""

        media_div = soup.select_one("div#media")
        if media_div:
            img_tag = media_div.select_one("img[src*='@@images']") or media_div.find("img")
            if img_tag:
                src = img_tag.get("src") or img_tag.get("data-src", "")
                if src:
                    image_url = self._abs(src, url)
                
                nxt = img_tag.find_next_sibling("p", class_="discreet")
                caption = nxt.get_text(strip=True) if nxt else img_tag.get("alt", "").strip()
                if caption and caption.lower() in ("imagem", "image", "foto", "photo", ""):
                    caption = ""
                image_caption = caption[:500] if caption else ""

        if not image_url:
            content_area = (
                soup.select_one("div#content-core")
                or soup.select_one("div#content")
                or soup.select_one("article")
            )
            if content_area:
                img_tag = (
                    content_area.select_one("img[src*='@@images']")
                    or content_area.select_one("figure img")
                    or content_area.select_one("img.image-inline")
                    or content_area.select_one("img")
                )
                if img_tag:
                    src = img_tag.get("src") or img_tag.get("data-src", "")
                    if src:
                        image_url = self._abs(src, url)

                    caption = img_tag.get("alt", "").strip()
                    if caption and caption.lower() in ("imagem", "image", "foto", "photo", ""):
                        caption = ""
                    if not caption:
                        figcap = img_tag.find_parent("figure")
                        if figcap:
                            fc = figcap.select_one("figcaption")
                            if fc:
                                caption = fc.get_text(strip=True)
                    if not caption:
                        nxt = img_tag.find_next_sibling()
                        if getattr(nxt, "name", None) in ("p", "span", "em"):
                            caption = nxt.get_text(strip=True)
                        elif isinstance(nxt, NavigableString):
                            caption = str(nxt).strip()
                    image_caption = caption[:500] if caption else ""

        category = ""
        cat_div = soup.select_one("div#form-widgets-categoria")
        if cat_div:
            category = cat_div.get_text(strip=True)

        if not category:
            for p in soup.select("p, div, span"):
                txt = p.get_text(separator="\n", strip=True)
                lines = [l.strip() for l in txt.split("\n") if l.strip()]
                if lines and lines[0].lower() == "categoria" and len(lines) > 1:
                    category = lines[1]
                    break

        tags = []
        for tag_el in soup.select("a[rel='tag'], a.link-category, #category a"):
            t_text = tag_el.get_text(strip=True)
            if t_text and t_text not in tags:
                tags.append(t_text)

        return NewsItem(
            title=title,
            body=body_html,
            url=url,
            summary=summary,
            date=date,
            image_url=image_url,
            image_caption=image_caption,
            category=category,
            tags=tags,
        )


def main():
    global CONFIG, log
    
    # Se não foi configurado via módulo, tenta carregar config.json local
    if not CONFIG:
        CONFIG_FILE = "config.json"
        if not os.path.exists(CONFIG_FILE):
            print(f"Erro: Arquivo '{CONFIG_FILE}' não encontrado!")
            return
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            CONFIG = json.load(f)
            
    # Garante que o log esteja configurado se não houver handlers
    if not logging.getLogger().handlers:
        setup_log()
    
    log.info("=" * 65)
    log.info("  Trensurb (Plone 4) → Plone 6  |  Migração de Notícias")
    log.info("=" * 65)

    done = load_progress(CONFIG["progress_file"])
    if done:
        log.info(f"▶️  Retomando: {len(done)} URL(s) já migradas anteriormente.")

    try:
        plone = PloneClient(
            CONFIG["plone_url"],
            CONFIG["plone_token"],
        )
    except Exception as e:
        log.error(f"Falha na autenticação Plone 6: {e}")
        return

    scraper = TrensurbScraper(CONFIG)
    links   = scraper.get_links()
    if not links:
        log.error("Nenhum link encontrado.")
        return

    folder = CONFIG["plone_news_folder"]
    if folder:
        plone.ensure_folder_exists(folder)

    stats  = {"ok": 0, "skip": 0, "dup": 0, "error": 0}

    for i, link in enumerate(links, 1):
        if STOP_SIGNAL:
            log.warning("🛑 Interrupção solicitada pelo usuário. Finalizando com segurança...")
            break
            
        log.info(f"\n[{i}/{len(links)}] {link}")

        if link in done:
            log.info("   ⏭️  Já migrado (progresso salvo). Pulando.")
            stats["skip"] += 1
            continue

        time.sleep(CONFIG["delay"])

        news = scraper.scrape(link)
        if not news:
            stats["error"] += 1
            continue

        # --- Resolve URL do item de destino ---
        migrate_as_self = CONFIG.get("migrate_as_self", False)

        if migrate_as_self:
            # Modo: A própria pasta/página de destino recebe os blocos diretamente.
            folder_api_url = plone._api_id(f"{plone.base_url}{folder}")
            item_url = folder_api_url
            log.info(f"   🎯 [migrate_as_self] PATCH direto na pasta: {folder}")
        else:
            title_check = news.title.strip() if news.title else "Página sem Título - Migração"
            existing_url = plone.get_content_url(folder, title_check)

            if existing_url:
                log.info("   ⚠️  Página Base já existe. Retomando processamento (Smart Resume)...")
                item_url = existing_url
            else:
                base_result = plone.create_empty_news(folder, news)
                if not base_result:
                    stats["error"] += 1
                    continue
                item_url = base_result.get("@id")
                if not item_url:
                    stats["error"] += 1
                    continue


        # Início processamento de arquivos anexos no corpo
        skip_files = CONFIG.get("skip_files", False)
        soup_body = BeautifulSoup(news.body, "lxml")

        if skip_files:
            log.info("   ⏩ [skip_files=true] Resolvendo links sem re-fazer upload...")
            file_exts = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".rar")
            resolved = 0
            for a in soup_body.find_all("a"):
                href = a.get("href", "")
                if not href:
                    continue
                href_lower = href.lower()
                
                # Detecção mais agressiva de arquivo
                is_file = any(href_lower.endswith(ext) for ext in file_exts) or \
                          "/@@download/" in href_lower or \
                          "/at_download/" in href_lower or \
                          "/download/" in href_lower or \
                          "download" in href_lower

                if not is_file:
                    continue

                # Recria a mesma lógica de nome do upload_file_attachment
                fn = href.split("/")[-1].split("?")[0]
                if not fn or fn.lower() in ("download", "view", "at_download", "file", ""):
                    import hashlib
                    h = hashlib.md5(href.encode()).hexdigest()[:8]
                    fn = f"arquivo_{h}.pdf"

                if "pdf" not in fn.lower() and (".pdf" in href_lower or "pdf" in href_lower):
                    fn += ".pdf"
                
                if not any(fn.lower().endswith(ext) for ext in file_exts):
                     fn += ".pdf"

                # Busca o arquivo já existente no Plone pelo nome
                existing_url = plone.get_file_url(folder, fn)
                if existing_url:
                    # EXTRAI O CAMINHO COMPLETO DO SITE (ex: /trensurb/pt-br/folder/file)
                    from urllib.parse import urlparse
                    site_path = urlparse(existing_url).path
                    a["href"] = f"{site_path}/@@display-file/file"
                    resolved += 1
                else:
                    log.warning(f"   ⚠️  Arquivo não encontrado p/ resolver link: {fn}")
            log.info(f"   🔗 {resolved} link(s) resolvido(s) (Site-Relative Paths).")
        else:
            file_exts = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".rar")
            for a in soup_body.find_all("a"):
                href = a.get("href", "")
                if href:
                    href_lower = href.lower()
                    is_file = any(href_lower.endswith(ext) for ext in file_exts) or \
                              "/@@download/" in href_lower or \
                              "/at_download/" in href_lower or \
                              "download" in href_lower
                    
                    if is_file and ("gov.br" in href_lower or href_lower.startswith("http")):
                        new_url = plone.upload_file_attachment(folder, href, scraper.sess)
                        if new_url != href:
                            from urllib.parse import urlparse
                            site_path = urlparse(new_url).path
                            a["href"] = f"{site_path}/@@display-file/file"
                            log.info(f"   🔗 Link atualizado (Site-Relative): {a['href']}")


        # --- Injeção Automática de Acessibilidade (WCAG) ---
        for a in soup_body.find_all("a"):
            if not a.get_text(strip=True) and not a.get("aria-label") and not a.get("title"):
                img_tag = a.find("img")
                desc = img_tag.get("alt", "").strip() if img_tag else ""

                if not desc:
                    from urllib.parse import urlparse, unquote
                    h = a.get("href", "")
                    if "@@display-file/file" in h or "@@download/file" in h:
                        # URL já migrada para o Plone: pega o segmento antes da action do arquivo
                        nome = re.split(r"/@@(?:display-file|download)/file", h, maxsplit=1)[0].rstrip("/").split("/")[-1]
                    else:
                        # URL original (gov.br ou relativa): usar o último segmento do path
                        parsed_path = urlparse(h).path
                        nome = unquote(parsed_path.rstrip("/").split("/")[-1])
                        # Formata: remove extensão e deixa legível
                        base = nome.rsplit(".", 1)[0] if "." in nome else nome
                        nome = base.replace("-", " ").replace("_", " ").title() or nome

                    desc = f"Acessar {nome}" if nome else "Acessar link"

                a["title"] = desc
                a["aria-label"] = desc

                # Injeta span oculto com texto para que o conversor Slate capture
                if not a.get_text(strip=True):
                    new_span = soup_body.new_tag("span")
                    new_span["class"] = "sr-only"
                    new_span.string = desc
                    a.append(new_span)
        # ---------------------------------------------------


        body_wrapper = soup_body.find("body")
        if body_wrapper:
            news.body = "".join([str(c) for c in body_wrapper.children])
        else:
            news.body = str(soup_body)
        # Fim processamento arquivos

        patch_ok = plone.patch_news_blocks(item_url, news)
        if patch_ok:
            stats["ok"] += 1
            done.add(link)
            save_progress(CONFIG["progress_file"], done)
            
            # Executa apenas se for imagem real e tipo for compatível (evita erros em Documents sem block pra imagem nativa lead)
            if news.image_url and CONFIG.get("portal_type", "News Item") == "News Item":
                plone.upload_image(item_url, news.image_url, news.image_caption)
        else:
            stats["error"] += 1

    log.info("\n" + "=" * 65)
    log.info(f"  ✅ Migradas com sucesso : {stats['ok']}")
    log.info(f"  ⏭️ Puladas (progresso)  : {stats['skip']}")
    log.info(f"  🔁 Já existiam no Plone : {stats['dup']}")
    log.info(f"  ❌ Erros                : {stats['error']}")
    log.info(f"  📄 Log completo         : migracao.log")
    log.info(f"  💾 Progresso salvo em   : {CONFIG['progress_file']}")
    log.info("=" * 65)


if __name__ == "__main__":
    main()
