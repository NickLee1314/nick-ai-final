import os
import time
import threading
import schedule
import gradio as gr
from supabase import create_client, Client
from duckduckgo_search import DDGS

# 載入 Google GenAI 並進行環境兼容
try:
    import google.genai as genai
    from google.genai.types import HttpOptions  # ✅ 導入最新連線設定
except ImportError:
    try:
        from google import genai
        from google.genai.types import HttpOptions
    except ImportError:
        import genai
        from google.genai.types import HttpOptions

# --- 1. 讀取雲端金鑰與多用戶設定 ---
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# 解析多用戶
users_env = os.environ.get("WEB_USERS", "admin:admin")
AUTH_LIST = []
if users_env:
    for pair in users_env.split(','):
        if ':' in pair:
            parts = pair.split(':', 1)
            if len(parts) == 2:
                username = parts[0].strip()
                password = parts[1].strip()  # ✅ 確保密碼讀取正確
                if username and password:
                    AUTH_LIST.append((username, password))

supabase = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

# ✅【終極修復關鍵】強制命令 Client 走最新的 "v1" 穩定通道，徹底擺脫 v1beta 404 限制！
client = None
if GEMINI_API_KEY:
    client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=HttpOptions(api_version="v1")  # 🚀 強制指定 v1 通道，阻斷 404 錯誤
    )

# --- 2. 雲端資料庫操作 ---
def get_user_profile(username):
    if not supabase: return "無"
    try:
        res = supabase.table('user_profile').select('fact').eq('username', username).execute()
        facts = [item['fact'] for item in res.data]
        return "\n".join([f"- {f}" for f in facts]) if facts else "無"
    except: return "無"

def update_user_profile(user_input, username):
    if not user_input or not supabase or not client: return
    try:
        prompt = f"分析這句話：「{user_input}」。是否透露了說話者的個人偏好、物品 or 習慣？有則總結事實，無則回覆『無』。"
        # ✅ 使用最新一代、性價比最高且保證支援的 gemini-2.5-flash 
        resp = client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
        fact = resp.text.strip()
        if fact != "無" and "沒有" not in fact and len(fact) > 2:
            supabase.table('user_profile').insert({"fact": fact, "username": username}).execute()
    except: pass 

def recall_long_term_memory(username):
    if not supabase: return []
    try:
        res = supabase.table('memory').select('*').eq('username', username).order('id', desc=True).limit(30).execute()
        return res.data
    except: return []

def search_the_web(query):
    if not query: return "無文字查詢"
    try:
        results = DDGS().text(query, max_results=3)
        return "".join([f"【來源】{res['title']}\n摘要: {res['body']}\n" for res in results])
    except Exception:
        return "網路上暫時無法搜尋。"

# --- 3. 核心大腦邏輯 ---
def ask_smart_agent(user_text, uploaded_files, history, username):
    if not client or not supabase:
        return "⚠️ 系統尚未設定金鑰，請至 Settings 中設定。"

    short_term = ""
    for user_msg, ai_msg in history[-3:]:
        if isinstance(user_msg, tuple):
            u_text = "[上傳了檔案]"
        elif isinstance(user_msg, dict):
            u_text = user_msg.get("text", "[上傳了檔案]")
        else:
            u_text = str(user_msg)
        short_term += f"我:{u_text}\n你:{ai_msg}\n"
        
    profile = get_user_profile(username)
    long_term_mems = recall_long_term_memory(username)
    long_term_context = "".join([f"舊紀錄:{m['question']} -> {m['answer'][:100]}...\n" for m in long_term_mems])
    web_context = search_the_web(user_text)
    
    prompt = f"""你是一個多模態專屬 AI 助理。當前服務的主人是：{username}。
【主人長期特徵】\n{profile}
【本次對話上下文】\n{short_term if short_term else "新對話。"}
【歷史記憶】\n{long_term_context if long_term_context else "無。"}
【網路最新資訊】\n{web_context}
【目前提問/指示】\n{user_text}
請給出精準回答："""
    
    contents_to_send = [prompt]
    uploaded_g_files = [] 
    
    if uploaded_files:
        for filepath in uploaded_files:
            file_size_mb = os.path.getsize(filepath) / (1024 * 1024)
            if file_size_mb > 10:
                return f"⚠️ 警告：檔案 ({file_size_mb:.1f}MB) 超過 10MB 限制！"
            try:
                g_file = client.files.upload(file=filepath)
                contents_to_send.append(g_file)
                uploaded_g_files.append(g_file) 
            except Exception as e:
                return f"❌ 檔案上傳失敗: {e}"

    try:
        # ✅ 使用最新一代、效能強大的 gemini-2.5-flash
        response = client.models.generate_content(model='gemini-2.5-flash', contents=contents_to_send)
        final_answer = response.text
        
        db_question = user_text if user_text else "[分析了上傳的檔案]"
        supabase.table('memory').insert({"question": db_question, "answer": final_answer, "username": username}).execute()
        update_user_profile(user_text, username)
        
        return final_answer
        
    except Exception as e:
        return f"⚠️ AI 大腦暫時無法思考 (可能為安全攔截、金鑰無效或 API 限制)：{str(e)}"
        
    finally:
        for gf in uploaded_g_files:
            try:
                client.files.delete(name=gf.name)
            except: pass

# --- 4. 建立 Gradio 多模態網頁 ---
def chat_logic(message_dict, history, request: gr.Request):
    username = request.username if request and hasattr(request, "username") and request.username else "admin"
    raw_text = message_dict.get("text", "")
    text = raw_text.strip()
    files = message_dict.get("files", [])
    
    normalized_text = text.replace(" ", "").replace("　", "").replace("。", "").replace(".", "").replace("！", "").replace("!", "")

    if not text and files:
        text = "請幫我分析我上傳的檔案/錄音檔。"

    yield "⏳ 系統正在處理中，請稍候..."
    
    if not supabase:
        yield "⚠️ 錯誤：無法連線至 Supabase 資料庫，請檢查金鑰設定。"
        return

    if normalized_text == "清除所有對話紀錄":
        supabase.table('memory').delete().eq('username', username).execute()
        yield f"🧹 [{username}] 的所有歷史對話記憶已被永久刪除！"
        
    elif normalized_text == "清除我的個人畫像":
        supabase.table('user_profile').delete().eq('username', username).execute()
        yield f"🧹 [{username}] 的長期個人偏好筆記已被徹底清空！"
        
    elif normalized_text.startswith("設定目標：") or normalized_text.startswith("設定目標:"):
        target = raw_text.split("：")[-1].split(":")[-1].strip()
        supabase.table('research_targets').insert({"target": target, "username": username}).execute()
        yield f"🎯 [{username}] 的目標已設定：『{target}』"
        
    elif normalized_text == "自主學習":
        res = supabase.table('research_targets').select('target').eq('username', username).order('id', desc=True).limit(1).execute()
        target_str = res.data[0]['target'] if res.data else "2026年AI最新突破"
        yield f"🚀 正在為 [{username}] 針對『{target_str}』進行深度學習..."
        yield ask_smart_agent(f"針對『{target_str}』搜尋最新趨勢並整理報告。", [], history, username)
        
    else:
        yield ask_smart_agent(text, files, history, username)

demo = gr.ChatInterface(
    fn=chat_logic, 
    multimodal=True, 
    title="🚀 可進化 AI 助理 V12.1 (終極相容正式版)",
    description="具備多用戶隔離與 RAG 記憶的頂級架構。<br>👇 **請手動輸入，或點擊下方的【快捷指令按鈕】：**",
    examples=[
        [{"text": "自主學習"}],
        [{"text": "清除所有對話紀錄"}],
        [{"text": "清除我的個人畫像"}],
        [{"text": "設定目標：2026年人工智慧發展趨勢"}]
    ]
)

# --- 5. 全自動背景排程 ---
def daily_background_learning():
    print("⏰ [定時任務] 啟動每日全自動自主學習...")
    if not supabase or not client: return
    try:
        res = supabase.table('research_targets').select('username, target').execute()
        if not res.data: return
        unique_users = {row['username']: row['target'] for row in res.data}
        for user, target in unique_users.items():
            print(f"🔄 正在為用戶 [{user}] 學習目標: {target}")
            ask_smart_agent(f"請針對『{target}』進行今日的最新進度總結報告。", [], [], user)
            time.sleep(10)
        print("✅ [定時任務] 今日全自動學習完成！")
    except Exception as e:
        print(f"背景任務錯誤: {e}")

def run_scheduler():
    schedule.every().day.at("03:00").do(daily_background_learning)
    while True:
        schedule.run_pending()
        time.sleep(60)

threading.Thread(target=run_scheduler, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    if AUTH_LIST:
        demo.launch(server_name="0.0.0.0", server_port=port, auth=AUTH_LIST)
    else:
        demo.launch(server_name="0.0.0.0", server_port=port)
