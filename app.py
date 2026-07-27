"""
纺织客服 Web UI
===============
FastAPI + 原生 HTML，模仿企业微信界面

运行: python app.py → http://127.0.0.1:8000
"""

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pathlib import Path
from langchain_core.messages import HumanMessage
from src.agent import build_graph
from src.memory import get_user

app = FastAPI(title="宏润纺织 AI 客服")
agent_graph = build_graph()

USER_ID = "123456"
memory = get_user(USER_ID)


class ChatRequest(BaseModel):
    message: str


@app.get("/", response_class=HTMLResponse)
def index():
    return """
<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>宏润纺织 AI 客服</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'PingFang SC','Hiragino Sans GB',sans-serif;background:#ededed;height:100vh;display:flex;justify-content:center}
#app{width:100%;max-width:500px;height:100vh;display:flex;flex-direction:column;background:#fff}
#header{background:#07c160;color:#fff;padding:12px 16px;font-size:17px;font-weight:600;text-align:center}
#chat{flex:1;overflow-y:auto;padding:12px;background:#f5f5f5}
.msg{margin-bottom:12px;display:flex}
.msg.user{justify-content:flex-end}
.msg.ai{justify-content:flex-start}
.bubble{max-width:80%;padding:10px 14px;border-radius:8px;font-size:14px;line-height:1.6;word-break:break-word}
.msg.user .bubble{background:#95ec69;color:#000;border-radius:8px 4px 8px 8px}
.msg.ai .bubble{background:#fff;color:#333;border-radius:4px 8px 8px 8px;box-shadow:0 1px 2px rgba(0,0,0,.08)}
.msg.ai .bubble table{border-collapse:collapse;margin:8px 0;font-size:12px}
.msg.ai .bubble td,.msg.ai .bubble th{border:1px solid #ddd;padding:4px 8px;text-align:left}
.msg.ai .bubble pre{white-space:pre-wrap;font-family:inherit;font-size:13px;margin:0}
#input-area{display:flex;padding:10px;border-top:1px solid #e0e0e0;background:#fff}
#input{flex:1;padding:10px 14px;border:1px solid #e0e0e0;border-radius:20px;font-size:15px;outline:none}
#send{width:60px;margin-left:8px;background:#07c160;color:#fff;border:none;border-radius:20px;font-size:15px;cursor:pointer}
.loading{color:#999;font-size:12px;padding:4px 0}
</style>
</head>
<body>
<div id="app">
  <div id="header">🏭 宏润纺织 AI 客服</div>
  <div id="chat"></div>
  <div id="input-area">
    <input id="input" placeholder="输入消息..." autofocus>
    <button id="send" onclick="send()">发送</button>
  </div>
</div>
<script>
const chat = document.getElementById('chat');
const input = document.getElementById('input');

async function send() {
  const msg = input.value.trim();
  if (!msg) return;
  input.value = '';
  addBubble('user', msg);
  const loading = addBubble('ai', '<span class="loading">...</span>', true);
  try {
    const resp = await fetch('/chat', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({message: msg})
    });
    const data = await resp.json();
    loading.remove();
    addBubble('ai', data.reply);
  } catch(e) {
    loading.remove();
    addBubble('ai', '网络异常，请稍后重试');
  }
}

function addBubble(role, text) {
  const div = document.createElement('div');
  div.className = 'msg ' + role;
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.innerHTML = text.replace(/\\n/g,'<br>').replace(/\\*\\*(.*?)\\*\\*/g,'<b>$1</b>');
  div.appendChild(bubble);
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  return div;
}

input.addEventListener('keydown', e => { if (e.key === 'Enter') send(); });

// 加载历史
(async () => {
  const resp = await fetch('/history');
  const data = await resp.json();
  data.forEach(h => addBubble(h.role === 'human' ? 'user' : 'ai', h.content));
})();
</script>
</body>
</html>"""


@app.post("/chat")
def chat(req: ChatRequest):
    try:
        # 加载历史 + 偏好
        history = memory.load_recent(20)
        prefs = memory.retrieve_preferences()
        user_context = "；".join(prefs) if prefs else ""

        messages = history + [HumanMessage(content=req.message)]
        last_type = memory.get_last_query_type()

        state = {"messages": messages, "knowledge_chunks": [], "rewrite_query": "",
                 "query_type": last_type, "user_id": USER_ID, "user_context": user_context}
        result = agent_graph.invoke(state)

        # 保存本轮状态供下一轮延续
        memory.save_last_query_type(result.get("query_type", "chat"))

        # 存档 + 异步提取
        memory.save_messages([HumanMessage(content=req.message), result["messages"][-1]])
        import threading
        from src.agent import cheap_llm
        threading.Thread(target=memory.extract_and_store, args=(result["messages"], cheap_llm), daemon=True).start()

        return {"reply": result["messages"][-1].content}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"reply": f"系统异常: {str(e)[:200]}, 请稍后重试"}


@app.get("/history")
def get_history():
    rows = memory.load_recent(30)
    return [{"role": m.type, "content": m.content} for m in rows]


if __name__ == "__main__":
    import uvicorn
    print("="*50)
    print("🏭 宏润纺织 AI 客服 Web 版")
    print("   打开 http://127.0.0.1:8003")
    print("="*50)
    uvicorn.run(app, host="0.0.0.0", port=8003, log_level="warning")
