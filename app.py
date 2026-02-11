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
from collections import defaultdict

app = Flask(__name__)

# ========== 설정 ==========
SPREADSHEET_ID = os.environ.get('SPREADSHEET_ID', '')
SLACK_BOT_TOKEN = os.environ.get('SLACK_BOT_TOKEN', '')
SLACK_CHANNEL_ID = os.environ.get('SLACK_CHANNEL_ID', '')
GOOGLE_CREDENTIALS = os.environ.get('GOOGLE_CREDENTIALS', '')

# User-Agent 설정
PC_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
MOBILE_USER_AGENT = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
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


def get_products_from_sheet():
    """구글 시트의 '제품' 시트에서 제품명+키워드+찾을이름 읽어오기"""
    try:
        client = get_google_sheet_client()
        if not client:
            return []
        
        spreadsheet = client.open_by_key(SPREADSHEET_ID)
        
        try:
            product_sheet = spreadsheet.worksheet("제품")
        except:
            print("'제품' 시트를 찾을 수 없습니다.")
            return []
        
        all_data = product_sheet.get_all_values()
        
        if not all_data:
            return []
        
        # 헤더 제외
        if all_data[0][0] in ["제품명", "제품", "product", "Product"]:
            all_data = all_data[1:]
        
        products = []
        for row in all_data:
            if len(row) >= 3 and row[0].strip() and row[1].strip() and row[2].strip():
                product_data = {
                    "product": row[0].strip(),
                    "keyword": row[1].strip(),
                    "search_name": row[2].strip()
                }
                products.append(product_data)
        
        return products
        
    except Exception as e:
        print(f"제품 읽기 오류: {e}")
        return []


def create_driver(is_mobile=False):
    """Chrome 드라이버 생성 (PC 또는 모바일)"""
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-blink-features=AutomationControlled")
    
    if is_mobile:
        options.add_argument("--window-size=375,812")
        options.add_argument(f"user-agent={MOBILE_USER_AGENT}")
    else:
        options.add_argument("--window-size=1920,1080")
        options.add_argument(f"user-agent={PC_USER_AGENT}")
    
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


def check_powerlink_single(driver, keyword, search_name, is_mobile=False):
    """단일 키워드 파워링크 노출 확인"""
    result = {
        "found": False,
        "position": None
    }
    
    try:
        encoded_keyword = urllib.parse.quote(keyword)
        
        # PC는 search.naver.com, 모바일은 m.search.naver.com
        if is_mobile:
            url = f"https://m.search.naver.com/search.naver?query={encoded_keyword}"
        else:
            url = f"https://search.naver.com/search.naver?where=nexearch&query={encoded_keyword}"
        
        driver.get(url)
        time.sleep(3)
        
        # 찾을이름으로 검색
        xpath_query = f"//*[contains(text(), '{search_name}')]"
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
            
            if search_name in ad_text and not result["found"]:
                result["found"] = True
                result["position"] = position
        
    except Exception as e:
        print(f"오류: {e}")
    
    return result


def check_powerlink(pc_driver, mobile_driver, product_data):
    """파워링크 노출 확인 (PC + 모바일)"""
    product = product_data["product"]
    keyword = product_data["keyword"]
    search_name = product_data["search_name"]
    
    # PC 체크
    pc_result = check_powerlink_single(pc_driver, keyword, search_name, is_mobile=False)
    
    # 모바일 체크
    mobile_result = check_powerlink_single(mobile_driver, keyword, search_name, is_mobile=True)
    
    result = {
        "product": product,
        "keyword": keyword,
        "search_name": search_name,
        "pc_found": pc_result["found"],
        "pc_position": pc_result["position"],
        "mobile_found": mobile_result["found"],
        "mobile_position": mobile_result["position"]
    }
    
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
            result_sheet.append_row(["번호", "제품명", "키워드", "찾을이름", "PC노출", "PC순위", "모바일노출", "모바일순위", "확인시간"])
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
                r["product"],
                r["keyword"],
                r["search_name"],
                "노출" if r["pc_found"] else "미노출",
                f"{r['pc_position']}위" if r["pc_position"] else "-",
                "노출" if r["mobile_found"] else "미노출",
                f"{r['mobile_position']}위" if r["mobile_position"] else "-",
                check_time
            ]
            result_sheet.append_row(row)
            
    except Exception as e:
        print(f"시트 저장 오류: {e}")


def send_slack_notification(results):
    """슬랙 봇으로 알림 전송 - 표 형식"""
    if not SLACK_BOT_TOKEN or not SLACK_CHANNEL_ID:
        return
    
    try:
        check_date = datetime.now().strftime("%Y-%m-%d")
        
        # 제품별로 그룹화
        products = defaultdict(list)
        
        for r in results:
            product_name = r["product"]
            products[product_name].append(r)
        
        message = f"🔍 *파워링크 노출 확인* ({check_date})\n\n"
        
        for product_name, items in products.items():
            message += f"📦 *{product_name}*\n"
            message += "```\n"
            message += "┌────────────────┬────────┬────────┐\n"
            message += "│ 키워드           │ PC     │ 모바일  │\n"
            message += "├────────────────┼────────┼────────┤\n"
            
            for r in items:
                keyword = r['keyword']
                # 키워드 길이 맞추기 (최대 14자)
                if len(keyword) > 14:
                    keyword = keyword[:13] + "…"
                keyword = keyword.ljust(14)
                
                # PC 순위
                if r["pc_found"] and r["pc_position"]:
                    pc_text = f"{r['pc_position']}위 ✅"
                else:
                    pc_text = "❌"
                pc_text = pc_text.center(6)
                
                # 모바일 순위
                if r["mobile_found"] and r["mobile_position"]:
                    mobile_text = f"{r['mobile_position']}위 ✅"
                else:
                    mobile_text = "❌"
                mobile_text = mobile_text.center(6)
                
                message += f"│ {keyword} │ {pc_text} │ {mobile_text} │\n"
            
            message += "└────────────────┴────────┴────────┘\n"
            message += "```\n\n"
        
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
    pc_driver = None
    mobile_driver = None
    try:
        products = get_products_from_sheet()
        
        if not products:
            return jsonify({
                "status": "error", 
                "message": "제품이 없습니다. 구글 시트의 '제품' 시트를 확인하세요."
            }), 400
        
        # PC, 모바일 드라이버 생성
        pc_driver = create_driver(is_mobile=False)
        mobile_driver = create_driver(is_mobile=True)
        
        results = []
        
        for product_data in products:
            result = check_powerlink(pc_driver, mobile_driver, product_data)
            results.append(result)
            time.sleep(1)
        
        save_to_google_sheet(results)
        send_slack_notification(results)
        
        return jsonify({
            "status": "success",
            "products_count": len(products),
            "results": results,
            "timestamp": datetime.now().isoformat()
        })
        
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
        
    finally:
        if pc_driver:
            pc_driver.quit()
        if mobile_driver:
            mobile_driver.quit()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
