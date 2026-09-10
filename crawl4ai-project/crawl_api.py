#!/usr/bin/env python3
"""
HTML Cleaner for Business Research & Lead Generation
- No LLM, no AI, no summarization.
- Only deterministic HTML cleaning and structured extraction.
- Processes ONLY the exact URLs provided in the request.
- Preserves all meaningful content: headings, paragraphs, lists, nested text.
- Removes only technical noise (scripts, styles, ads, cookie banners, tracking).
- Extracts social, phone, email, address from raw HTML BEFORE cleaning.
- Supports DuckDuckGo search result extraction.
- FIXED: Enhanced regex-based email detection (mailto href, text, JSON-LD, Meta).
- FIXED: Parallel crawling and parallel processing for multiple URLs.
- FIXED: Pydantic V2 .model_dump() instead of deprecated .dict()
"""

import asyncio
import json
import re
import logging
import os
import time
from typing import List, Optional, Dict, Any
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn
from bs4 import BeautifulSoup
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig

# Google Maps business listing scraper (same Crawl4AI project, dedicated module).
from maps_scraper import scrape_google_maps

# ── Logging Setup ──────────────────────────────────────────────────────────
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "crawl_api.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ── FastAPI App ────────────────────────────────────────────────────────────
app = FastAPI(title="HTML Cleaner for Lead Generation")

# ── Request/Response Models ──────────────────────────────────────────────
class CleanRequest(BaseModel):
    urls: List[str]

class HeaderInfo(BaseModel):
    company_name: Optional[str] = None
    tagline: Optional[str] = None
    phone: List[str] = []
    email: List[str] = []
    address: Optional[str] = None
    social_links: List[Dict[str, str]] = []

class MainContentInfo(BaseModel):
    title: Optional[str] = None
    headings: List[str] = []
    paragraphs: List[str] = []
    lists: List[str] = []
    full_text: Optional[str] = None

class FooterInfo(BaseModel):
    company_information: Optional[str] = None
    contact_information: Optional[str] = None
    opening_hours: Optional[Dict[str, str]] = None
    legal_information: Optional[str] = None
    social_links: List[Dict[str, str]] = []

class MetadataInfo(BaseModel):
    phones: List[str] = []
    emails: List[str] = []
    social_links: List[Dict[str, str]] = []

class CleanResponse(BaseModel):
    url: str
    success: bool
    header: HeaderInfo = HeaderInfo()
    main_content: MainContentInfo = MainContentInfo()
    footer: FooterInfo = FooterInfo()
    metadata: MetadataInfo = MetadataInfo()
    error: Optional[str] = None

class RawResponse(BaseModel):
    url: str
    success: bool
    html: Optional[str] = None
    markdown: Optional[str] = None
    fit_markdown: Optional[str] = None
    links: List[Dict[str, str]] = []
    media: List[Dict[str, str]] = []
    error: Optional[str] = None

# ── Google Maps request/response models ──────────────────────────────────
class MapsRequest(BaseModel):
    # Business type to search (e.g. "digital marketing agencies").
    query: str
    # Optional location to restrict the search (e.g. "New York").
    location: Optional[str] = None
    # Optional cap on the number of businesses to collect.
    max_results: Optional[int] = 25

class BusinessResult(BaseModel):
    name: str
    website: str = ""
    phone: str = ""
    address: str = ""
    rating: str = ""
    review_count: str = ""

# ── Crawl4AI Configuration ──────────────────────────────────────────────
browser_config = BrowserConfig(headless=True, verbose=False)
run_config = CrawlerRunConfig(
    page_timeout=30000,
    cache_mode="bypass",
)

# ── Helper Functions ──────────────────────────────────────────────────────

def clean_url(url: str) -> str:
    if not url:
        return url
    try:
        parsed = urlparse(url)
        track_params = {'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
                        'fbclid', 'gclid', 'ref', 'ref_src', 'mc_cid', 'mc_eid'}
        query_dict = parse_qs(parsed.query)
        clean_dict = {k: v for k, v in query_dict.items() if k.lower() not in track_params}
        new_query = urlencode(clean_dict, doseq=True)
        cleaned = parsed._replace(query=new_query)
        return urlunparse(cleaned)
    except:
        return url

def extract_json_ld(soup: BeautifulSoup) -> List[dict]:
    scripts = soup.find_all('script', type='application/ld+json')
    data = []
    for script in scripts:
        try:
            content = script.string
            if content:
                parsed = json.loads(content)
                if isinstance(parsed, list):
                    data.extend(parsed)
                else:
                    data.append(parsed)
        except:
            continue
    return data

def extract_emails_from_jsonld(jsonld_data: List[dict]) -> List[str]:
    emails = []
    for item in jsonld_data:
        if not isinstance(item, dict):
            continue
        if 'email' in item and isinstance(item['email'], str):
            emails.append(item['email'])
        if 'contactPoint' in item:
            cp = item['contactPoint']
            if isinstance(cp, list):
                for c in cp:
                    if isinstance(c, dict) and 'email' in c:
                        emails.append(c['email'])
            elif isinstance(cp, dict) and 'email' in cp:
                emails.append(cp['email'])
    return list(set(emails))

def extract_emails_from_meta(soup: BeautifulSoup) -> List[str]:
    emails = []
    meta_email = soup.find('meta', attrs={'name': re.compile(r'email', re.I)})
    if meta_email and meta_email.get('content'):
        emails.append(meta_email['content'].strip())
    meta_og_email = soup.find('meta', attrs={'property': re.compile(r'og:email', re.I)})
    if meta_og_email and meta_og_email.get('content'):
        emails.append(meta_og_email['content'].strip())
    return list(set(emails))

def find_company_name_from_jsonld(jsonld_data: List[dict]) -> Optional[str]:
    for item in jsonld_data:
        if not isinstance(item, dict):
            continue
        types = item.get('@type', [])
        if isinstance(types, str):
            types = [types]
        for t in types:
            if 'Organization' in t or 'Business' in t or 'Company' in t:
                name = item.get('name')
                if name:
                    return name
        if item.get('@type') == 'Article' and 'publisher' in item:
            publisher = item.get('publisher')
            if isinstance(publisher, dict) and publisher.get('name'):
                return publisher.get('name')
    return None

def extract_company_name(soup: BeautifulSoup, url: str) -> Optional[str]:
    jsonld = extract_json_ld(soup)
    name = find_company_name_from_jsonld(jsonld)
    if name:
        return name
    meta_site = soup.find('meta', attrs={'property': 'og:site_name'})
    if meta_site and meta_site.get('content'):
        return meta_site['content'].strip()
    meta_app = soup.find('meta', attrs={'name': 'application-name'})
    if meta_app and meta_app.get('content'):
        return meta_app['content'].strip()
    logo = soup.find('img', class_=re.compile(r'logo|brand', re.I))
    if logo and logo.get('alt'):
        alt = logo['alt'].strip()
        if len(alt) > 2 and 'logo' in alt.lower():
            return alt
    for img in soup.find_all('img', src=True):
        src = img.get('src', '').lower()
        if 'logo' in src or 'brand' in src:
            alt = img.get('alt', '').strip()
            if alt:
                return alt
    footer = soup.find('footer')
    if footer:
        footer_text = footer.get_text(separator=' ', strip=True)
        lines = footer_text.split('\n')
        for line in lines:
            line = line.strip()
            if line and ('©' in line or 'copyright' in line.lower()):
                match = re.search(r'©\s*(.+?)(?:\s+\d|$)', line, re.I)
                if match:
                    return match.group(1).strip()
    parsed = urlparse(url)
    domain = parsed.netloc
    if domain.startswith('www.'):
        domain = domain[4:]
    parts = domain.split('.')
    if len(parts) >= 2:
        return parts[0].capitalize()
    return None

def extract_tagline(soup: BeautifulSoup) -> Optional[str]:
    meta_desc = soup.find('meta', attrs={'name': 'description'})
    if meta_desc and meta_desc.get('content'):
        desc = meta_desc['content'].strip()
        if len(desc) > 20:
            return desc
    meta_og = soup.find('meta', attrs={'property': 'og:description'})
    if meta_og and meta_og.get('content'):
        return meta_og['content'].strip()
    return None

def extract_phone_numbers(text: str) -> List[str]:
    patterns = [
        r'(\+?\d{1,3}[\s-]?)?\(?\d{2,4}\)?[\s-]?\d{3,4}[\s-]?\d{3,4}',
        r'(\+?\d{1,3}[\s-]?)?\d{3,4}[\s-]?\d{3,4}[\s-]?\d{3,4}'
    ]
    phones = []
    for pattern in patterns:
        matches = re.findall(pattern, text)
        for m in matches:
            if isinstance(m, tuple):
                m = ''.join(m)
            cleaned = re.sub(r'[\s\-()]', '', m)
            if len(cleaned) >= 7:
                phones.append(m.strip())
    seen = set()
    unique = []
    for p in phones:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique

def extract_emails(text: str) -> List[str]:
    email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    raw_emails = re.findall(email_pattern, text, re.IGNORECASE)
    cleaned_emails = [e.rstrip('.,;:)"\'>') for e in raw_emails]
    return list(set(cleaned_emails))

def extract_emails_from_href(html_str: str) -> List[str]:
    emails = []
    pattern = re.compile(r'(?i)href\s*=\s*["\']mailto:([^"\'\s?]+)')
    for match in pattern.finditer(html_str):
        email = match.group(1).strip()
        if re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', email, re.IGNORECASE):
            emails.append(email)
    return list(set(emails))

def extract_contact_from_mailto_tel(soup: BeautifulSoup) -> Dict[str, List[str]]:
    emails = []
    phones = []
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        href_lower = href.lower()
        if href_lower.startswith('mailto:'):
            email = href[7:].split('?')[0].strip()
            if email:
                emails.append(email)
        elif href_lower.startswith('tel:'):
            phone = href[4:].strip()
            if phone:
                phones.append(phone)
    return {'emails': list(set(emails)), 'phones': list(set(phones))}

def extract_social_links_from_soup(soup: BeautifulSoup) -> List[Dict[str, str]]:
    platform_map = {
        'facebook.com': 'facebook', 'fb.com': 'facebook',
        'instagram.com': 'instagram', 'youtube.com': 'youtube',
        'youtu.be': 'youtube', 'linkedin.com': 'linkedin',
        'twitter.com': 'twitter', 'x.com': 'x', 'tiktok.com': 'tiktok'
    }
    social_links = []
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if not href:
            continue
        cleaned = clean_url(href)
        if cleaned.startswith('/') or cleaned.startswith('#'):
            continue
        for domain, platform in platform_map.items():
            if domain in cleaned.lower():
                if any(x in cleaned.lower() for x in ['shop', 'store', 'product', 'affiliate', 'track']):
                    continue
                social_links.append({"platform": platform, "url": cleaned})
                break
    seen = set()
    unique = []
    for link in social_links:
        if link['url'] not in seen:
            seen.add(link['url'])
            unique.append(link)
    return unique

def extract_address_strict(soup: BeautifulSoup) -> Optional[str]:
    address_tag = soup.find('address')
    if address_tag:
        text = address_tag.get_text(separator=' ', strip=True)
        if text:
            return text
    jsonld = extract_json_ld(soup)
    for item in jsonld:
        if isinstance(item, dict):
            if item.get('@type') == 'PostalAddress' or 'PostalAddress' in item.get('@type', []):
                street = item.get('streetAddress', '')
                locality = item.get('addressLocality', '')
                region = item.get('addressRegion', '')
                postal = item.get('postalCode', '')
                country = item.get('addressCountry', '')
                parts = [p for p in [street, locality, region, postal, country] if p]
                if parts:
                    return ', '.join(parts)
            if 'Organization' in str(item.get('@type', '')) or 'LocalBusiness' in str(item.get('@type', '')):
                address = item.get('address')
                if isinstance(address, dict):
                    street = address.get('streetAddress', '')
                    locality = address.get('addressLocality', '')
                    region = address.get('addressRegion', '')
                    postal = address.get('postalCode', '')
                    country = address.get('addressCountry', '')
                    parts = [p for p in [street, locality, region, postal, country] if p]
                    if parts:
                        return ', '.join(parts)
    address_classes = ['contact', 'address', 'location', 'find-us']
    for cls in address_classes:
        for elem in soup.find_all(class_=re.compile(cls, re.I)):
            text = elem.get_text(separator=' ', strip=True)
            if text and len(text) > 10:
                if not elem.find_all('a', href=True):
                    return text
        for elem in soup.find_all(id=re.compile(cls, re.I)):
            text = elem.get_text(separator=' ', strip=True)
            if text and len(text) > 10:
                if not elem.find_all('a', href=True):
                    return text
    for iframe in soup.find_all('iframe', src=True):
        src = iframe['src']
        if 'maps.google' in src or 'google.com/maps' in src:
            parent = iframe.find_parent()
            if parent:
                text = parent.get_text(separator=' ', strip=True)
                if text:
                    return text
    for a in soup.find_all('a', href=True):
        href = a['href']
        if 'google.com/maps' in href or 'maps.google' in href:
            text = a.get_text(strip=True)
            if text:
                return text
            parent = a.find_parent()
            if parent:
                text = parent.get_text(separator=' ', strip=True)
                if text:
                    return text
    footer = soup.find('footer')
    if footer:
        for cls in address_classes:
            for elem in footer.find_all(class_=re.compile(cls, re.I)):
                text = elem.get_text(separator=' ', strip=True)
                if text and len(text) > 10:
                    if not elem.find_all('a', href=True):
                        return text
        footer_text = footer.get_text(separator=' ', strip=True)
        address_pattern = r'\d{1,5}\s+[A-Za-z0-9\s,]+(?:Street|St|Avenue|Ave|Road|Rd|Lane|Ln|Drive|Dr|Court|Ct|Place|Pl|Way|Boulevard|Blvd)'
        match = re.search(address_pattern, footer_text, re.I)
        if match:
            return match.group(0).strip()
    return None

def extract_opening_hours(text: str) -> Optional[Dict[str, str]]:
    if not text:
        return None
    lines = text.split('\n')
    hours = {}
    day_map = {
        'mon': 'Monday', 'tue': 'Tuesday', 'wed': 'Wednesday',
        'thu': 'Thursday', 'fri': 'Friday', 'sat': 'Saturday', 'sun': 'Sunday'
    }
    pattern = re.compile(
        r'(?P<day>(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:day)?)\s*[:.]?\s*(?P<time>[\d:apm\s\-]+)',
        re.I
    )
    for line in lines:
        line = line.strip()
        if not line:
            continue
        match = pattern.search(line)
        if match:
            day_abbr = match.group('day').lower()
            day = day_map.get(day_abbr[:3], None)
            if day:
                time_str = match.group('time').strip()
                time_str = re.sub(r'\s+', ' ', time_str)
                hours[day] = time_str
    if hours:
        ordered = {}
        for day in ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']:
            if day in hours:
                ordered[day] = hours[day]
        return ordered
    return None

def remove_noise(soup: BeautifulSoup) -> None:
    for tag in soup.find_all(['script', 'style', 'iframe', 'noscript']):
        tag.decompose()
    for tag in soup.find_all(attrs={"hidden": True}):
        tag.decompose()
    for tag in soup.find_all(style=re.compile(r'display\s*:\s*none', re.I)):
        tag.decompose()
    noise_patterns = [
        r'ad[s]?', r'banner', r'popup', r'modal', r'overlay',
        r'cookie', r'consent', r'gdpr', r'newsletter', r'promo',
        r'tracking', r'analytics', r'facebook[-_]?plugin', r'twitter[-_]?timeline'
    ]
    for pattern in noise_patterns:
        for tag in soup.find_all(class_=re.compile(pattern, re.I)):
            tag.decompose()
        for tag in soup.find_all(id=re.compile(pattern, re.I)):
            tag.decompose()
    rec_patterns = [
        r'recommend', r'related', r'trending', r'suggested',
        r'more[-_]?from', r'you[-_]?may[-_]?also', r'popular[-_]?posts',
        r'audio[-_]?player', r'playlist', r'widget', r'subscribe', r'signup'
    ]
    for pattern in rec_patterns:
        for tag in soup.find_all(class_=re.compile(pattern, re.I)):
            tag.decompose()
        for tag in soup.find_all(id=re.compile(pattern, re.I)):
            tag.decompose()
    for tag in soup.find_all():
        if not tag.get_text(strip=True) and not tag.find_all(['img', 'a', 'input', 'button']):
            tag.decompose()

def extract_main_content(soup: BeautifulSoup, url: str) -> MainContentInfo:
    main_info = MainContentInfo()
    title_tag = soup.find('title')
    if title_tag:
        main_info.title = title_tag.get_text(strip=True)
    else:
        og_title = soup.find('meta', attrs={'property': 'og:title'})
        if og_title and og_title.get('content'):
            main_info.title = og_title['content'].strip()
        else:
            h1 = soup.find('h1')
            if h1:
                main_info.title = h1.get_text(strip=True)
    headings = []
    for heading_tag in ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']:
        for heading in soup.find_all(heading_tag):
            text = heading.get_text(strip=True)
            if text:
                headings.append(text)
    main_info.headings = headings
    paragraphs = []
    lists = []
    for p in soup.find_all('p'):
        text = p.get_text(strip=True)
        if text and len(text) > 5:
            paragraphs.append(text)
    for li in soup.find_all('li'):
        text = li.get_text(strip=True)
        if text and len(text) > 3:
            lists.append(text)
    full_parts = []
    for elem in soup.find_all(['h1','h2','h3','h4','h5','h6','p','li','blockquote','figcaption','caption','th','td']):
        if elem.parent and elem.parent.name in ['script','style','noscript']:
            continue
        text = elem.get_text(strip=True)
        if text and len(text) > 3:
            full_parts.append(text)
    main_info.full_text = '\n\n'.join(full_parts) if full_parts else None
    main_info.paragraphs = paragraphs
    main_info.lists = lists
    return main_info

def extract_header_info(soup: BeautifulSoup, url: str) -> HeaderInfo:
    header = HeaderInfo()
    header.company_name = extract_company_name(soup, url)
    header.tagline = extract_tagline(soup)
    header_section = soup.find('header')
    if not header_section:
        body = soup.find('body')
        if body:
            for child in body.children:
                if child.name and child.get_text(strip=True):
                    header_section = child
                    break
    if header_section:
        header_text = header_section.get_text(separator=' ', strip=True)
        header.email = extract_emails(header_text)
        header.phone = extract_phone_numbers(header_text)
        contact_links = extract_contact_from_mailto_tel(header_section)
        header.email.extend(contact_links['emails'])
        header.phone.extend(contact_links['phones'])
        header.email = list(set(header.email))
        header.phone = list(set(header.phone))
    address = extract_address_strict(soup)
    if address:
        header.address = address
    return header

def extract_footer_info(soup: BeautifulSoup) -> FooterInfo:
    footer = FooterInfo()
    footer_tag = soup.find('footer')
    if not footer_tag:
        footer_candidates = soup.find_all(class_=re.compile(r'footer|bottom', re.I))
        if footer_candidates:
            footer_tag = footer_candidates[-1]
    if not footer_tag:
        return footer
    footer_copy = BeautifulSoup(str(footer_tag), 'lxml')
    for nav in footer_copy.find_all('nav'):
        nav.decompose()
    for ul in footer_copy.find_all('ul'):
        lis = ul.find_all('li')
        if len(lis) > 3:
            has_contact = False
            for li in lis:
                text = li.get_text(strip=True)
                if re.search(r'@|tel:|phone|address|street|email', text, re.I):
                    has_contact = True
                    break
            if not has_contact:
                ul.decompose()
    for div in footer_copy.find_all(class_=re.compile(r'language|lang', re.I)):
        div.decompose()
    footer_text = footer_copy.get_text(separator='\n', strip=True)
    lines = footer_text.split('\n')
    company_lines = []
    contact_lines = []
    opening_lines = []
    legal_lines = []
    contact_keywords = ['phone', 'email', 'address', 'tel', 'contact', 'mail']
    opening_keywords = ['opening', 'hours', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun', 'open', 'close']
    legal_keywords = ['privacy', 'terms', 'legal', 'cookie', 'policy', 'disclaimer']
    for line in lines:
        line = line.strip()
        if not line:
            continue
        lower = line.lower()
        if any(kw in lower for kw in legal_keywords):
            legal_lines.append(line)
        elif any(kw in lower for kw in opening_keywords):
            opening_lines.append(line)
        elif any(kw in lower for kw in contact_keywords):
            contact_lines.append(line)
        else:
            company_lines.append(line)
    footer.company_information = '\n'.join(company_lines) if company_lines else None
    footer.contact_information = '\n'.join(contact_lines) if contact_lines else None
    if opening_lines:
        hours_text = '\n'.join(opening_lines)
        hours_dict = extract_opening_hours(hours_text)
        footer.opening_hours = hours_dict
    footer.legal_information = '\n'.join(legal_lines) if legal_lines else None
    return footer

async def clean_html(html: str, url: str) -> Dict[str, Any]:
    if not html:
        return {
            "header": HeaderInfo(),
            "main_content": MainContentInfo(),
            "footer": FooterInfo(),
            "metadata": MetadataInfo()
        }

    raw_soup = BeautifulSoup(html, 'lxml')

    social_links_raw = extract_social_links_from_soup(raw_soup)
    all_text_raw = raw_soup.get_text(separator=' ', strip=True)
    phones_raw = extract_phone_numbers(all_text_raw)
    emails_raw = extract_emails(all_text_raw)
    
    contact_links_raw = extract_contact_from_mailto_tel(raw_soup)
    phones_raw.extend(contact_links_raw['phones'])
    emails_raw.extend(contact_links_raw['emails'])
    
    emails_href = extract_emails_from_href(html)
    emails_raw.extend(emails_href)
    
    jsonld_raw = extract_json_ld(raw_soup)
    emails_jsonld = extract_emails_from_jsonld(jsonld_raw)
    emails_raw.extend(emails_jsonld)
    
    emails_meta = extract_emails_from_meta(raw_soup)
    emails_raw.extend(emails_meta)

    phones_raw = list(set(phones_raw))
    emails_raw = list(set(emails_raw))
    
    address_raw = extract_address_strict(raw_soup)

    clean_soup = BeautifulSoup(html, 'lxml')
    remove_noise(clean_soup)

    header_info = extract_header_info(clean_soup, url)
    footer_info = extract_footer_info(clean_soup)
    main_info = extract_main_content(clean_soup, url)

    header_info.social_links = social_links_raw
    header_info.phone = phones_raw
    header_info.email = emails_raw
    if address_raw:
        header_info.address = address_raw

    footer_info.social_links = social_links_raw

    metadata = MetadataInfo(
        phones=phones_raw,
        emails=emails_raw,
        social_links=social_links_raw
    )

    return {
        "header": header_info,
        "main_content": main_info,
        "footer": footer_info,
        "metadata": metadata
    }

# ── DUCKDUCKGO PROCESSOR ──────────────────────────────────────────────────
def clean_title(title: str) -> str:
    if not title:
        return ""
    title = title.replace("**", "").replace("__", "")
    title = title.replace("*", "").strip()
    title = re.sub(r"\s+", " ", title).strip()
    return title

def clean_description(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)
    text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)
    text = text.replace("**", "").replace("__", "").replace("*", "").replace("_", " ")
    noise_patterns = [
        r"(?i)People also ask.*?(?=\n|$)",
        r"(?i)Viewing ads is privacy protected by DuckDuckGo.*?(?=\n|$)",
        r"(?i)Ad clicks are managed by Microsoft.*?(?=\n|$)",
        r"(?i)^\s*Ad\s*$",
        r"(?i)^\s*Sponsored\s*$",
        r"(?i)Find related content",
        r"(?i)About the source",
        r"(?i)What people are saying",
        r"\[Read more\]\([^)]+\)",
        r"\[More at.*?\]\([^)]+\)",
        r"\[Learn more\]\([^)]+\)",
        r"\[All\]\([^)]+\)",
    ]
    for pat in noise_patterns:
        text = re.sub(pat, "", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^[\s|,;:.]+|[\s|,;:.]+$", "", text)
    return text[:1000]

def extract_ddg_ai_overview(markdown: str) -> Optional[str]:
    pattern = re.compile(r"(?is)(?:^|\n)#+\s*(?:AI Answer|AI Assist).*?(?=\n#+\s|\n\Z)")
    match = pattern.search(markdown)
    if match:
        text = match.group(0).strip()
        text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > 30:
            return text[:5000]
    lines = markdown.split("\n")
    in_blockquote = False
    blockquote_lines = []
    for line in lines[:60]:
        if re.match(r"^\s*>\s*", line):
            in_blockquote = True
            blockquote_lines.append(re.sub(r"^\s*>\s*", "", line).strip())
        elif in_blockquote:
            if line.strip() and not line.startswith(">") and not line.startswith("#"):
                continue
            if not line.strip():
                break
    if blockquote_lines:
        text = " ".join(blockquote_lines).strip()
        if len(text) > 50:
            return text[:5000]
    return None

def extract_ddg_organic_results(markdown: str) -> List[Dict[str, str]]:
    results = []
    seen_urls = set()
    lines = markdown.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^#+\s+\[([^\]]+)\]\(([^)]+)\)", line)
        if m:
            title = m.group(1).strip()
            url = m.group(2).strip()
            if url in seen_urls or re.match(r"^https?://[^/]+/i[cp]", url):
                i += 1
                continue
            is_ad = False
            for k in range(i + 1, min(i + 3, len(lines))):
                peek = lines[k].strip()
                if peek == "Ad" or peek.startswith("Viewing ads"):
                    is_ad = True
                    break
                if peek and not peek.startswith("[") and not peek.startswith("!["):
                    break
            if is_ad:
                i += 1
                continue
            seen_urls.add(url)
            desc = ""
            for j in range(i + 1, min(i + 6, len(lines))):
                nl = lines[j].strip()
                if not nl:
                    continue
                if nl.startswith("![") or nl.startswith("[ !") or nl.startswith("[ www"):
                    continue
                if nl.startswith("["):
                    close_bracket = nl.rfind("](")
                    if close_bracket > 0:
                        nl = nl[1:close_bracket]
                desc = nl
                break
            if not desc:
                i += 1
                continue
            results.append({
                "title": clean_title(title),
                "url": url,
                "description": clean_description(desc),
            })
        i += 1
    return results

def process_duckduckgo(markdown: str) -> Dict:
    ai_overview = extract_ddg_ai_overview(markdown)
    organic_results = extract_ddg_organic_results(markdown)
    return {
        "ai_overview": ai_overview,
        "results": organic_results
    }

# ── API Endpoints (PARALLELIZED) ─────────────────────────────────────────

@app.post("/clean")
async def clean_endpoint(req: CleanRequest):
    if not req.urls:
        raise HTTPException(400, "Provide 'urls' array")
    logger.info(f"Received /clean request with {len(req.urls)} URL(s)")
    
    async with AsyncWebCrawler(config=browser_config) as crawler:
        # Step 1: Crawl all URLs in parallel using arun_many
        logger.info(f"Starting parallel crawl for {len(req.urls)} URLs...")
        crawl_results = await crawler.arun_many(urls=req.urls, config=run_config)
        
        # Step 2: Define parallel processing logic for each crawled result
        async def process_clean_result(url: str, crawl_result):
            entry = {"url": url, "success": False}
            try:
                if not crawl_result or not crawl_result.success:
                    error_msg = crawl_result.error_message if crawl_result else "Crawl failed"
                    logger.error(f"Crawl failed for {url}: {error_msg}")
                    entry["error"] = error_msg
                    return entry
                
                html = crawl_result.html
                if not html:
                    logger.warning(f"No HTML content for {url}")
                    entry["error"] = "Empty HTML content"
                    return entry

                parsed = urlparse(url)
                if 'duckduckgo.com' in parsed.netloc.lower() and parsed.query:
                    logger.info(f"Detected DuckDuckGo search URL: {url}")
                    md_obj = crawl_result.markdown
                    markdown = str(md_obj.raw_markdown) if md_obj else ""
                    if not markdown:
                        logger.warning(f"No markdown for DDG URL {url}")
                        entry["error"] = "Empty markdown"
                        return entry
                    ddg_output = process_duckduckgo(markdown)
                    entry["success"] = True
                    entry["type"] = "duckduckgo"
                    entry["ai_overview"] = ddg_output["ai_overview"]
                    entry["results"] = ddg_output["results"]
                    logger.info(f"DDG extraction completed for {url}")
                    return entry

                # Normal cleaning pipeline
                cleaned = await clean_html(html, url)
                entry["success"] = True
                # 🛠️ FIX: Use .model_dump() instead of deprecated .dict() for Pydantic V2
                entry["header"] = cleaned["header"].model_dump()
                entry["main_content"] = cleaned["main_content"].model_dump()
                entry["footer"] = cleaned["footer"].model_dump()
                entry["metadata"] = cleaned["metadata"].model_dump()
                logger.info(f"Successfully cleaned {url}")
            except Exception as e:
                logger.exception(f"Error processing {url}: {str(e)}")
                entry["error"] = str(e)
            return entry

        # Step 3: Run processing for all websites in parallel
        tasks = [process_clean_result(url, result) for url, result in zip(req.urls, crawl_results)]
        results = await asyncio.gather(*tasks)
        
    logger.info(f"Completed /clean request for {len(req.urls)} URL(s)")
    return list(results)


# ── GOOGLE MAPS BUSINESS LISTING ENDPOINT ──────────────────────────────────

@app.post("/maps")
async def maps_endpoint(req: MapsRequest) -> List[BusinessResult]:
    """
    Search Google Maps for a business type (optionally within a location) and
    return a clean list of {name, website, phone, address, rating,
    review_count} for each business found.

    Example: {"query": "digital marketing agencies", "location": "New York"}
    """
    if not req.query or not req.query.strip():
        raise HTTPException(400, "Provide 'query' (business type)")
    if req.max_results is not None and req.max_results < 1:
        raise HTTPException(400, "'max_results' must be >= 1")

    logger.info(
        f"Received /maps request: query='{req.query}' location='{req.location}' max_results={req.max_results}"
    )

    # The scraped result list is already deduped and contains only real,
    # visitable websites (sponsored ad-tracker rows are dropped).
    businesses = await scrape_google_maps(
        req.query, location=req.location, max_results=req.max_results
    )

    # Return the full listing details for each business.
    return [
        BusinessResult(
            name=b["name"],
            website=b.get("website") or "",
            phone=b.get("phone") or "",
            address=b.get("address") or "",
            rating=b.get("rating") or "",
            review_count=b.get("review_count") or "",
        )
        for b in businesses
    ]


if __name__ == "__main__":
    logger.info("Starting HTML Cleaner API server (no LLM, no crawling beyond provided URLs)")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
