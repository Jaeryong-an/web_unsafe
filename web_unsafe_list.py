import os, re, time, datetime, requests, base64, io
import streamlit as st
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.service_account import Credentials
from webdriver_manager.chrome import ChromeDriverManager
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from PIL import Image
import pytesseract
import gspread
import openai
from fugashi import Tagger
import json

# Google認証とAPIキー
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
SHEET_NAME = os.getenv("SHEET_NAME")
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID")
GPT_API_KEY = os.getenv("OPENAI_API_KEY")

# ✅ 필수 환경변수 체크
missing_vars = []
for var_name in ["SERVICE_ACCOUNT_JSON", "SPREADSHEET_ID", "SHEET_NAME", "DRIVE_FOLDER_ID", "OPENAI_API_KEY"]:
    if not os.getenv(var_name):
        missing_vars.append(var_name)
if missing_vars:
    st.error(f"❌ 次の変数が設定されてません。: {', '.join(missing_vars)}")
    st.stop()

# ✅ 인증 처리
try:
    service_account_info = json.loads(SERVICE_ACCOUNT_JSON)
except Exception:
    st.error("❌ SERVICE_ACCOUNT_JSON が有効なJSONではありません。")
    st.stop()

creds = Credentials.from_service_account_info(
    service_account_info,
    scopes=[
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/spreadsheets"
    ]
)

gc = gspread.authorize(creds)
drive_service = build('drive', 'v3', credentials=creds)
worksheet = gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)

# ✅ OpenAI 설정
openai.api_key = GPT_API_KEY

@st.cache_resource
def validate_openai_key(api_key):
    if not api_key:
        return False
    try:
        headers = {"Authorization": f"Bearer {api_key}"}
        resp = requests.get("https://api.openai.com/v1/models", headers=headers, timeout=5)
        return resp.status_code == 200
    except Exception:
        return False

def load_rules_from_sheet(sheet_name: str):
    worksheet = gc.open_by_key(SPREADSHEET_ID).worksheet(sheet_name)
    rows = worksheet.get_all_values()[1:] 

    genre_keywords = {}
    url_patterns = {}
    japanese_domains = []

    for row in rows:
        if len(row) < 4:
            continue

        genre = row[0].strip()
        rule_type = row[1].strip().lower()
        rule_base = row[2].strip() if len(row) >= 3 else ""
        rule_regex = row[3].strip() if len(row) >= 4 else ""

        rule = rule_regex if rule_regex else rule_base
        if not rule:
            continue

        if rule_type == "keyword":
            genre_keywords.setdefault(genre, []).append(rule)
        elif rule_type == "pattern":
            url_patterns.setdefault(genre, []).append(rule)
        elif rule_type == "jpdomain":
            japanese_domains.append(rule.lower())

    return genre_keywords, url_patterns, japanese_domains

# OCR + クリーン本文抽出
tagger = Tagger()

def get_words(text):
    return [word.surface for word in tagger(text)] if text else []

def extract_domain(url):
    try:
        return urlparse(url).netloc.lower()
    except:
        return ""

def fetch_with_retry(url, timeout=10, retries=1):
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, timeout=timeout)
            resp.encoding = resp.apparent_encoding
            return resp
        except requests.exceptions.RequestException:
            if attempt < retries:
                time.sleep(1)
    return None

def extract_clean_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    ad_selectors = [
        'header', 'footer', 'nav', 'iframe', 'ins',
        '.footer', '.header', '.ads', '.sponsor', '.promo', '.widget',
        '[id*="ad"]', '[class*="ad"]', '[class*="sponsor"]', '[id*="sponsor"]',
        '[class*="banner"]', '[id*="banner"]', '[class*="rec"]', '[class*="promo"]'
    ]
    for selector in ad_selectors:
        for tag in soup.select(selector):
            tag.decompose()

    for tag in soup(["script", "style"]):
        tag.decompose()

    return soup.get_text(separator="\n", strip=True)


GENRE_KEYWORDS, URL_PATTERNS, JAPANESE_DOMAINS = load_rules_from_sheet("GenreRules")

def extract_image_ocr_text(img_path: str) -> str:
    try:
        img = Image.open(img_path)
        return pytesseract.image_to_string(img, lang='jpn+eng').strip()
    except:
        return ""

# スクショ
def take_fullpage_screenshot(driver, save_path):
    try:
        # JavaScriptの読み込み待ち（最大5秒）
        for _ in range(5):
            ready_state = driver.execute_script("return document.readyState")
            if ready_state == "complete":
                break
            time.sleep(1)

        # ページ全体のサイズ取得
        total_height = driver.execute_script("return Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)")
        total_width = driver.execute_script("return Math.max(document.body.scrollWidth, document.documentElement.scrollWidth)")

        driver.set_window_size(total_width, total_height)
        time.sleep(2)  # ウィンドウリサイズ反映待ち

        # スクロールしてからキャプチャすることで読み込みが安定
        driver.execute_script("window.scrollTo(0, 0)")
        time.sleep(0.5)
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight / 2)")
        time.sleep(0.5)
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(0.5)
        driver.execute_script("window.scrollTo(0, 0)")
        time.sleep(1.5)

        driver.save_screenshot(save_path)
        if os.path.exists(save_path):
            return save_path
        else:
            raise FileNotFoundError("スクリーンショット保存に失敗しました")
    except Exception as e:
        st.warning(f"📸 スクリーンショットエラー: {e}")
        return ""

def crawl_with_ocr(url: str, idx: int) -> tuple[str, str, str, str, str]:
    try:
        try:
            resp = requests.get(url, timeout=10)
            resp.encoding = resp.apparent_encoding
            maintext = extract_clean_text(resp.text)
        except Exception:
            maintext = ""

        shot_path = f"screenshot_{idx}.png"
        driver = None
        try:
            options = Options()
            options.add_argument('--headless') 
            options.add_argument('--disable-gpu')
            options.add_argument('--no-sandbox')
            options.add_argument('--disable-dev-shm-usage')
            options.add_argument('--window-size=1280,1500') 
            service = Service(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=options)
            driver.set_page_load_timeout(20)
            driver.get(url)
            time.sleep(2.5)
            html = driver.page_source
            shot_path = take_fullpage_screenshot(driver, shot_path)

            if not maintext:
                maintext = extract_clean_text(driver.page_source)

            soup = BeautifulSoup(driver.page_source, "html.parser")
            img = soup.find("img")
            img_src = img["src"] if img and img.get("src") else ""
            img_alt = img["alt"] if img and img.get("alt") else ""
            image_desc = img_alt or img_src

        except Exception:
            image_desc = ""
        finally:
            if driver:
                driver.quit()

        ocr_text = extract_image_ocr_text(shot_path if os.path.exists(shot_path) else "")
        image_combined_desc = image_desc + "\n" + ocr_text

        if not maintext.strip():
            maintext = "本文取得失敗"
        return maintext, image_combined_desc, shot_path, html if os.path.exists(shot_path) else "", ocr_text
    except Exception as e:
        return "本文取得失敗", "", ""

# キーワード及びジャンル判定
def judge_keywords_by_count(text, keywords_dict):
    results = []
    for genre, patterns in keywords_dict.items():
        matched = []
        for pattern in patterns:
            try:
                if re.search(pattern, text, re.IGNORECASE):
                    matched.append(pattern)
            except re.error:
                continue  # エラー無視
        if matched:
            results.append((genre, len(matched), matched))
    return results

def judge_genre_by_patterns(targets, patterns_dict):
    for genre, patterns in patterns_dict.items():
        for patt in patterns:
            patt_regex = re.escape(patt).replace(r'\*', '.*')
            for target in targets:
                if target and re.search(patt_regex, target, re.IGNORECASE):
                    return genre, patt
    return None, None

def judge_genre_final(url, text, image_desc, image_url, keywords_dict, url_patterns, gpt_fn=None):
    targets = [url, image_url, image_desc]
    genre, matched = judge_genre_by_patterns(targets, url_patterns)
    if genre:
        return f"[パターン/ドメイン一致] {genre}（パターン: {matched}）"
    text_for_kw = (text or "") + " " + (image_desc or "")
    keyword_matches = judge_keywords_by_count(text_for_kw, keywords_dict)
    if keyword_matches:
        lines = []
        for genre, count, matched in keyword_matches:
            lines.append(f"{genre}（{count}個一致） - {', '.join(matched[:5])}...")
        return "[キーワードマッチ]\n" + "\n".join(lines)
    if gpt_fn:
        return f"[GPT判定] {gpt_fn(text, image_desc, keywords_dict)}"
    return "判定不可"

# --- GPT ---
def gpt_judge_genre(maintext, image_desc, keywords_dict):
    if not GPT_API_KEY:
        return "GPT未実行"
    maintext_short = maintext[:800] if len(maintext) > 800 else maintext
    image_desc_short = image_desc[:300] if len(image_desc) > 300 else image_desc
    full_context = f"[本文（最大800字）]:\n{maintext_short}\n\n[画像の説明・OCR結果（最大300字）]:\n{image_desc_short}"

    genre_prompt = (
    "以下はあるウェブサイトの本文と画像に関する情報です。下記のジャンル定義に基づいて、どのカテゴリに該当するかを判断してください。\n"
    "複数該当しても構いません。\n"
    "該当なしの場合は「[ジャンル]: 要確認」と記載してください。\n\n"
    "【ジャンル定義とNG/OK例】\n"
    "1. アダルト：R18指定、性的な描写、肌の露出が多い、年齢制限のあるページ\n"
    "   NG例: ヌード画像、性行為描写、年齢確認ページあり\n"
    "   OK例: 健康や医療目的の性教育ページ\n\n"
    "2. 悪質CGM：ユーザー投稿型サイトで不適切なコメントや悪質投稿がされやすいもの\n"
    "   NG例: 2ちゃんねる、爆サイ、出会い系掲示板\n"
    "   OK例: 適切にモデレーションされている口コミサイト\n\n"
    "3. 著作権侵害：著作物（漫画・アニメ・映画・音楽など）を無断転載しているサイト\n"
    "   NG例: 漫画のスクショ掲載、タレントの画像多数転載、過度なネタバレ\n"
    "   OK例: 表紙画像のみ、感想のみ記載したブログ\n\n"
    "4. ポイント：ポイント付与や交換が主目的のサイト、またはその紹介\n"
    "   NG例: ポイントメール、ポイント情報・交換・紹介系サイト\n"
    "   OK例: 金融商品のレビューサイト（ポイント非重視）\n\n"
    "5. ヘイト：個人・人種・宗教・性別などに対する誹謗中傷や差別的表現\n"
    "   NG例: 特定集団への攻撃、差別用語の使用、職業批判、政治的煽動\n"
    "   OK例: 批判的だが冷静な報道\n\n"
    "6. 危険物：毒物、違法薬物、武器、爆発物、犯罪行為に関する情報\n"
    "   NG例: 銃器の販売、危険ドラッグの紹介、ハッキング方法の具体例\n"
    "   OK例: セキュリティ啓発、合法的な護身具の紹介\n\n"
    "7. グロテスク：ショッキングな表現、死体、解体映像、自殺事件など\n"
    "   NG例: 自殺現場の写真、昆虫食映像、排泄物、事故死映像\n"
    "   OK例: 医学的なコンテンツ、芸術的な写真作品\n\n"
    "8. ネガティブ：主にヘイトに該当しないが、愚痴・悲観・不安を煽る内容\n"
    "   NG例: 有名人以外への誹謗、個人へのネガティブ投稿、日記形式の愚痴\n"
    "   OK例: 一般的な不満共有、ニュースへのコメント\n\n"
    "9. 閲覧不可：アクセス不可、403/404エラー、内容が空の場合\n"
    "10. 認証が必要：ログインしないと閲覧できない、パスワード要求\n"
    "11. 海外サイト：外国語で書かれている、外国IPから運営されているサイト\n\n"
    f"{full_context}\n\n"
    f"[画像の説明・OCR結果]:\n{image_desc}\n\n"
    "【出力フォーマット】\n"
    "[ジャンル]: ○○（複数あればカンマ区切り）\n"
    "[理由]: ジャンル判定の根拠を簡潔に記載\n"
)
    try:
        response = openai.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": genre_prompt}],
            max_tokens=200,
            temperature=0.2,
        )
        content = response.choices[0].message.content.strip()
        genre_match = re.search(r"\[ジャンル\]:\s*([^\n/]+)", content)
        reason_match = re.search(r"\[理由\]:\s*(.+)", content)

        if genre_match:
            genre_text = genre_match.group(1).strip()
            if any(x in genre_text for x in ["要確認", "該当なし", "カテゴリー該当なし"]):
                return "[ジャンル]: カテゴリー該当なし"
            return f"[ジャンル]: {genre_text} / [理由]: {reason_match.group(1).strip() if reason_match else 'なし'}"

        return "[ジャンル]: カテゴリー該当なし"
    except Exception as e:
        return f"GPTエラー: {str(e)}"

def gpt_judge_image(image_path, alt_ocr_text=""):
    if not GPT_API_KEY or not os.path.exists(image_path):
        return "画像未解析"
    try:
        with open(image_path, "rb") as f:
            img_bytes = f.read()
        with Image.open(image_path) as img:
            img = img.convert("RGB")
            img.thumbnail((1600, 1600))  # リサイズ
            buffered = io.BytesIO()
            img.save(buffered, format="PNG")
            img_bytes = buffered.getvalue()
        base64_image = base64.b64encode(img_bytes).decode("utf-8")

        # base64 大きさのはみ出し防止
        if len(base64_image) > 4_000_000:
            return f"画像サイズ過大（{len(base64_image)}バイト）でGPT不可"

        messages = [
            {"role": "user", "content":
            "以下の画像とそのOCR/ALTテキストを分析し、該当するジャンルがあれば判定してください。\n"
            "明確に該当しない場合のみ [要確認] とし、少しでも該当の可能性があれば該当ジャンルを挙げてください。ジャンル名の後ろに補足があっても構いません。（例: アダルト（疑いあり））\n\n"
            "【ジャンル定義】\n"
            "- 閲覧不可：閲覧が制限されている表示\n"
            "- 認証が必要：ログインを求めるページ\n"
            "- アダルト：R18、性的な画像・警告表示\n"
            "- 悪質CGM：掲示板・不適切なコメント多数\n"
            "- 著作権侵害：動画・画像・漫画などの無断転載\n"
            "- ポイント：報酬付き広告、ポイント交換案内\n"
            "- ヘイト：差別表現や中傷がある\n"
            "- 危険物：薬物・毒物・暴力的な描写\n"
            "- グロテスク：死体・事故・自殺など\n"
            "- ネガティブ：悲惨な内容、絶望的表現\n"
            "- 海外サイト：英語など外国語表示のもの\n\n"
            f"[OCR/ALTテキストの一部]: {alt_ocr_text[:200]}\n"
            "【出力形式】\n"
            "[ジャンル]: ○○ / [理由]: ○○○○"
            },
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64_image}"}}
            ]}
        ]
        response = openai.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            max_tokens=200,
            temperature=0.3
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"画像GPTエラー: {str(e)}"

# サイト判断
def extract_body_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    body = soup.body
    if not body:
        return ""
    for selector in ['header', 'footer', 'nav', '.footer', '.header', '.ads', '[id*="ad"]', '[class*="ad"]']:
        for tag in body.select(selector):
            tag.decompose()
    for tag in body(["script", "style"]):
        tag.decompose()
    return body.get_text(separator="\n", strip=True)

def is_japanese_site_by_html(html: str, threshold: float = 0.4) -> bool:
    text = extract_body_text(html)
    if not text:
        return False
    total_chars = len(text)
    jp_chars = len(re.findall(r'[ぁ-んァ-ン一-龯]', text))
    return (jp_chars / total_chars) >= threshold if total_chars > 0 else False


def is_japanese_site_by_html_or_ocr(url: str, html: str, ocr_text: str, threshold: float = 0.4) -> bool:
    domain = extract_domain(url)
    if any(domain.endswith(jd) for jd in JAPANESE_DOMAINS):
        return True

    # HTML Body抽出
    text = extract_body_text(html)
    
    # HTML Bodyが少ない場合 → OCR テキストで変わって判定
    if not text or len(text.strip()) < 30:
        text = ocr_text or ""
    
    # 日本語文字の割合計算（ひらがな、カタカナ、漢字）
    total_chars = len(text)
    jp_chars = len(re.findall(r'[ぁ-んァ-ン一-龯]', text))

    # 英語しかなければ海外とみなす (逆補正)
    en_chars = len(re.findall(r'[a-zA-Z]', text))
    if total_chars > 0 and jp_chars == 0 and en_chars > total_chars * 0.2:
        return False

    return (jp_chars / total_chars) >= threshold if total_chars > 0 else False


# スコアリング
def compute_integrated_score(ocr_match_count, gpt_result_text, gpt_image_text):
    score = 0
    if ocr_match_count >= 4:
        score += 5
    elif ocr_match_count >= 2:
        score += 2.5
    elif ocr_match_count >= 1:
        score += 1.5
    if any(g in gpt_result_text for g in ['アダルト', '悪質CGM', '著作権侵害', 'ヘイト', '危険物', 'グロテスク']):
        score += 5
    gpt_image_genre_match = re.search(r"\[ジャンル\]:\\s*(.+?)\\s*(/|\\n|$)", gpt_image_text)
    if gpt_image_genre_match:
        gpt_image_genre = gpt_image_genre_match.group(1).strip()
        if gpt_image_genre in ['閲覧不可', '認証が必要', 'アダルト', '悪質CGM', '著作権侵害', 'ヘイト', '危険物', 'グロテスク']:
            score += 5
    return round(min(score, 15), 1)

# Googleドライブにアップロード
def upload_to_drive(file_path: str, file_name: str) -> str:
    try:
        # ✅ 업로드할 파일의 메타데이터 정의 (폴더 지정)
        file_metadata = {
            'name': file_name,
            'parents': [DRIVE_FOLDER_ID]  # 공유드라이브 내 폴더 ID
        }

        # ✅ 실제 파일 준비
        media = MediaFileUpload(file_path, resumable=True)

        # ✅ Google Drive에 파일 업로드 실행
        file = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id',
            supportsAllDrives=True
        ).execute()

        file_id = file.get('id')

        # ✅ 공개 권한 부여 (공유드라이브면 실패할 수도 있음 → 무시)
        try:
            drive_service.permissions().create(
                fileId=file_id,
                body={"role": "reader", "type": "anyone"},
                supportsAllDrives=True
            ).execute()
        except Exception as e:
            st.warning(f"⚠️ 公開権限の設定に失敗しましたが、親フォルダの設定により閲覧可能な場合は問題ありません。\n{e}")

        # ✅ Google Sheets에서 사용할 수 있는 공개 이미지 링크 반환
        return f"https://drive.google.com/uc?id={file_id}"

    except Exception as e:
        st.error(f"❌ Drive アップ失敗: {e}")
        return ""


# Streamlit UI
st.title("Web Unsafe 半定")
urls_input = st.text_area("対象URL一覧", height=200)

if st.button("判定実行"):
    urls = [u.strip() for u in urls_input.strip().split('\n') if u.strip()]
    progress = st.progress(0)
    status_text = st.empty()

    start_time = time.time()

    for idx, url in enumerate(urls, 1):
        try:
            maintext, image_desc, shot_path, html, ocr_text = crawl_with_ocr(url, idx)

            gpt_image_opinion = gpt_judge_image(shot_path, image_desc) if shot_path and os.path.isfile(shot_path) else "画像未解析"
            gpt_opinion = gpt_judge_genre(maintext, image_desc, GENRE_KEYWORDS) if GPT_API_KEY else "GPT未実行"

            if "要確認" in gpt_image_opinion or "判定できません" in gpt_image_opinion or "未解析" in gpt_image_opinion:
                preferred_opinion = gpt_opinion
            else:
                preferred_opinion = gpt_image_opinion

            drive_url = upload_to_drive(shot_path, os.path.basename(shot_path)) if shot_path and os.path.isfile(shot_path) else "結果なし"
            keyword_result = judge_keywords_by_count(maintext + " " + image_desc, GENRE_KEYWORDS)
            keyword_summary = "\n".join(f"{genre}（{count}個）: {', '.join(matched)}" for genre, count, matched in keyword_result)

            ocr_kw_count = sum(len(matched) for _, _, matched in keyword_result)
            ocr_point = 5 if ocr_kw_count >= 5 else 4 if ocr_kw_count >= 4 else 3 if ocr_kw_count >= 3 else 2 if ocr_kw_count >= 1 else 0
            gpt_point = 5 if any(g in gpt_opinion for g in ['アダルト', '悪質CGM', '著作権侵害', 'ヘイト', '危険物', 'グロテスク']) else 0
            gpt_image_point = 0
            genre_match = re.search(r"\[ジャンル\]:\s*(.+?)\s*(/|\n|$)", gpt_image_opinion)
            if genre_match and genre_match.group(1).strip() in ['閲覧不可', '認証が必要', 'アダルト', '悪質CGM', '著作権侵害', 'ヘイト', '危険物', 'グロテスク']:
                gpt_image_point = 5

            final_score = min(ocr_point + gpt_point + gpt_image_point, 15)

            cell = worksheet.find(url)
            row = cell.row

            site_origin = "日本サイト" if is_japanese_site_by_html_or_ocr(url, html, ocr_text) else "海外サイト"

            score_explanation = (
                f"{site_origin}\n"
                f"キーワードスコア: {ocr_point}点\n"
                f"GPT本文スコア: {gpt_point}点\n"
                f"GPT画像スコア: {gpt_image_point}点\n"
                f"[最終スコア]: {final_score}/15"
            )

            risk_level = "Unsafe" if final_score >= 11 else "NotSafe" if final_score >= 5 else "Safe"

            worksheet.update_cell(row, 2, datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))  # B
            worksheet.update_cell(row, 18, maintext[:1200])   # R
            worksheet.update_cell(row, 19, f'=IMAGE("{drive_url}", 1)')  # S
            worksheet.update_cell(row, 20, gpt_opinion)        # T
            worksheet.update_cell(row, 21, gpt_image_opinion)  # U
            worksheet.update_cell(row, 22, keyword_summary)    # V
            worksheet.update_cell(row, 23, score_explanation)  # W
            worksheet.update_cell(row, 24, risk_level)         # X
            # --- [Y列: ジャンル分類] 反映処理 --- #
            genre_final_match = re.search(r"\[ジャンル\]:\s*([^\n]+)", preferred_opinion)
            if genre_final_match:
                genre_final = genre_final_match.group(1).strip()
                if any(x in genre_final for x in ["要確認", "該当なし", "カテゴリー該当なし"]):
                    genre_final = "カテゴリー該当なし"
            else:
                genre_final = "カテゴリー該当なし"

            worksheet.update_cell(row, 25, genre_final)  # ✅ Y列

        except Exception as e:
            st.error(f"[{idx}] {url} の処理中にエラー発生: {str(e)}")
        finally:
            if shot_path and os.path.isfile(shot_path):
                try:
                    os.remove(shot_path)
                except:
                    pass

            elapsed = time.time() - start_time
            avg_time = elapsed / idx
            remaining = avg_time * (len(urls) - idx)
            rem_min, rem_sec = divmod(int(remaining), 60)
            status_text.text(f"進行中: {idx}/{len(urls)}件 ⏳ 残り予想: {rem_min}分{rem_sec}秒")
            progress.progress(idx / len(urls))

    total_time = time.time() - start_time
    total_min, total_sec = divmod(int(total_time), 60)
    st.success(f"判定完了 ✅ 所要時間: {total_min}分{total_sec}秒")