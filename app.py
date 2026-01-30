from flask import Flask, jsonify, request
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
import time
import urllib.parse
from datetime import datetime
import os
import json
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import requests

app = Flask(__name__)

# ========== 설정 ==========
SPREADSHEET_ID = os.environ.get('SPREADSHEET_ID', '')
SLACK_BOT_TOKEN = os.environ.get('SLACK_BOT_TOKEN', '')
SLACK_CHANNEL_ID = os.environ.get('SLACK_CHANNEL_ID', '')
GOOGLE_CREDENTIALS = os.environ.get('GOOGLE_CREDENTIALS', '')
# ==========================


def get_google_sheet_client():
    """구글 시트 클라이언트 생성"""
    if not GOOGLE_CREDENTIALS:
        return None
    
    creds_dict = json.loads(GOOGLE_CREDENTIALS)
    scope = [
        'https://spreadsheets.google.com/feeds',
        'https://www.googleapis.com/auth/drive'
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    return client


def get_keywords_from_sheet():
    """구글 시트의 '키워드' 시트에서 키워드+광고주+블로그ID 읽어오기"""
    try:
        client = get_google_sheet_client()
        if not client:
            return []
        
        spreadsheet = client.open_by_key(SPREADSHEET_ID)
        
        try:
            keyword_sheet = spreadsheet.worksheet("키워드")
        except:
            print("'키워드' 시트를 찾을 수 없습니다.")
            return []
        
        # 전체 데이터 읽기
        all_data = keyword_sheet.get_all_values()
        
        if not all_data:
            return []
        
        # 헤더 제외
        if all_data[0][0] in ["키워드", "keyword", "Keyword"]:
            all_data = all_data[1:]
        
        keywords = []
        for row in all_data:
            if len(row) >= 1 and row[0].strip():
                keyword_data = {
                    "keyword": row[0].strip(),
                    "advertiser": row[1].strip() if len(row) > 1 and row[1].strip() else "",
                    "blog_id": row[2].strip() if len(row) > 2 and row[2].strip() else ""
                }
                keywords.append(keyword_data)
        
        return keywords
        
    except Exception as e:
        print(f"키워드 읽기 오류: {e}")
        return []


def create_driver():
    """Chrome 드라이버 생성"""
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
    
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    return driver


def is_real_ad(text):
    """실제 광고인지 서브링크인지 판별"""
    sublink_patterns = [
        "제품소개", "회사소개", "납품사례", "고객문의", "견적문의",
        "안심저온가열", "UVC 살균", "대용량 수조", "무상AS",
        "상품보기", "이벤트", "공지사항", "오시는길", "문의하기",
        "브랜드소개", "제품안내", "서비스", "고객센터", "FAQ",
    ]
    
    text_clean = text.strip()
    
    if len(text_clean) < 12:
        return False
    
    for pattern in sublink_patterns:
        if text_clean.startswith(pattern) and len(text_clean) < 20:
            return False
    
    if ".com" in text_clean or ".co.kr" in text_clean or ".kr" in text_clean:
        return True
    
    if "네이버페이" in text_clean:
        return True
    
    if len(text_clean) > 30:
        return True
    
    return False


def check_powerlink(driver, keyword_data):
    """파워링크 노출 확인"""
    keyword = keyword_data["keyword"]
    advertiser = keyword_data["advertiser"]
    blog_id = keyword_data["blog_id"]
    
    result = {
        "keyword": keyword,
        "advertiser": advertiser,
        "blog_id": blog_id,
        "found": False,
        "position": None,
        "matched": None
    }
    
    try:
        encoded_keyword = urllib.parse.quote(keyword)
        url = f"https://search.naver.com/search.naver?where=nexearch&query={encoded_keyword}"
        driver.get(url)
        time.sleep(3)
        
        # 검색 조건 만들기
        search_terms = []
        if advertiser:
            search_terms.append(f"contains(text(), '{advertiser}')")
        if blog_id:
            search_terms.append(f"contains(text(), '{blog_id}')")
        
        if not search_terms:
            return result
        
        xpath_query = f"//*[{' or '.join(search_terms)}]"
        my_ad_elements = driver.find_elements(By.XPATH, xpath_query)
        
        if not my_ad_elements:
            return result
        
        my_li = None
        parent_ul = None
        
        for el in my_ad_elements:
            try:
                my_li = el.find_element(By.XPATH, "./ancestor::li")
                parent_ul = my_li.find_element(By.XPATH, "./parent::ul")
                break
            except:
                continue
        
        if not my_li or not parent_ul:
            result["found"] = True
            return result
        
        all_li = parent_ul.find_elements(By.CSS_SELECTOR, "li")
        
        real_ads = []
        for li in all_li:
            li_text = li.text.strip()
            if is_real_ad(li_text):
                real_ads.append({"text": li_text})
        
        for position, ad in enumerate(real_ads, 1):
            ad_text = ad["text"]
            
            is_my_ad = False
            matched_name = None
            
            if blog_id and blog_id in ad_text:
                is_my_ad = True
                matched_name = blog_id
            
            if not is_my_ad and advertiser and advertiser in ad_text:
                is_my_ad = True
                matched_name = advertiser
            
            if is_my_ad and not result["found"]:
                result["found"] = True
                result["position"] = position
                result["matched"] = matched_name
        
    except Exception as e:
        print(f"오류: {e}")
    
    return result


def save_to_google_sheet(results):
    """구글 시트의 '결과' 시트에 저장"""
    if not SPREADSHEET_ID or not GOOGLE_CREDENTIALS:
        return
    
    try:
        client = get_google_sheet_client()
        spreadsheet = client.open_by_key(SPREADSHEET_ID)
        
        try:
            result_sheet = spreadsheet.worksheet("결과")
        except:
            result_sheet = spreadsheet.add_worksheet(title="결과", rows=1000, cols=10)
        
        check_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        existing = result_sheet.get_all_values()
        if not existing:
            result_sheet.append_row(["번호", "키워드", "노출여부", "순위", "매칭", "광고주", "블로그", "확인시간"])
            next_num = 1
        else:
            try:
                last_num = int(existing[-1][0]) if existing[-1][0].isdigit() else 0
                next_num = last_num + 1
            except:
                next_num = len(existing)
        
        for i, r in enumerate(results):
            row = [
                next_num + i,
                r["keyword"],
                "노출" if r["found"] else "미노출",
                f"{r['position']}위" if r["position"] else "-",
                r.get("matched", "-") or "-",
                r.get("advertiser", "-") or "-",
                r.get("blog_id", "-") or "-",
                check_time
            ]
            result_sheet.append_row(row)
            
    except Exception as e:
        print(f"시트 저장 오류: {e}")


def send_slack_notification(results):
    """슬랙 봇으로 알림 전송"""
    if not SLACK_BOT_TOKEN or not SLACK_CHANNEL_ID:
        return
    
    try:
        found = sum(1 for r in results if r["found"])
        total = len(results)
        check_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        message = f"🔍 *파워링크 노출 확인 결과*\n"
        message += f"📅 {check_time}\n"
        message += f"📊 노출률: {found}/{total} ({round(found/total*100) if total > 0 else 0}%)\n\n"
        
        for r in results:
            status = "✅" if r["found"] else "❌"
            position = f"{r['position']}위" if r["position"] else "-"
            advertiser = r.get("advertiser", "")
            message += f"{status} [{advertiser}] {r['keyword']}: {position}\n"
        
        headers = {
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "channel": SLACK_CHANNEL_ID,
            "text": message
        }
        
        requests.post("https://slack.com/api/chat.postMessage", headers=headers, json=payload)
        
    except Exception as e:
        print(f"슬랙 알림 오류: {e}")


@app.route('/')
def home():
    return jsonify({"status": "ok", "message": "Powerlink Checker API"})


@app.route('/check')
def check():
    """파워링크 체크 실행"""
    driver = None
    try:
        keywords = get_keywords_from_sheet()
        
        if not keywords:
            return jsonify({
                "status": "error", 
                "message": "키워드가 없습니다. 구글 시트의 '키워드' 시트를 확인하세요."
            }), 400
        
        driver = create_driver()
        results = []
        
        for keyword_data in keywords:
            result = check_powerlink(driver, keyword_data)
            results.append(result)
            time.sleep(1.5)
        
        save_to_google_sheet(results)
        send_slack_notification(results)
        
        return jsonify({
            "status": "success",
            "keywords_count": len(keywords),
            "results": results,
            "timestamp": datetime.now().isoformat()
        })
        
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
        
    finally:
        if driver:
            driver.quit()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
