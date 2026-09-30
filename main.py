
import os
import requests
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")

HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Alpha AI</title>
    <style>
        body {
            margin: 0;
            font-family: Arial, sans-serif;
            background: #101114;
            color: white;
        }

        .header {
            padding: 20px;
            text-align: center;
            background: #181a20;
        }

        .header h1 {
            margin: 0;
            font-size: 28px;
        }

        .status {
            color: #35d07f;
            font-size: 13px;
            margin-top: 6px;
        }

        #chat {
            padding: 20px;
            max-width: 800px;
            margin: auto;
            min-height: 65vh;
        }

        .message {
            padding: 12px 15px;
            margin: 10px 0;
            border-radius: 14px;
            line-height: 1.5;
            white-space: pre-wrap;
        }

        .user {
            background: #315efb;
            margin-left: 15%;
        }

        .alpha {
            background: #20232b;
            margin-right: 15%;
        }

        .input-area {
            position: fixed;
            bottom: 0;
            left: 0;
            right: 0;
            padding: 12px;
            background: #181a20;
            display: flex;
            gap: 8px;
        }

        input {
            flex: 1;
            padding: 14px;
            border: none;
            border-radius: 12px;
            font-size: 16px;
            background: #292c35;
            color: white;
        }

        button {
            border: none;
            border-radius: 12px;
            padding: 0 20px;
            background: #315efb;
            color: white;
            font-size: 16px;
        }
    </style>
</head>

<body>

<div class="header">
    <h1>🤖 Alpha</h1>
    <div class="status">● READY TO HELP</div>
</div>

<div id="chat">
    <div class="message alpha">
        Hello! I'm Alpha. How can I help you?
    </div>
</div>

<div class="input-area">
    <input id="message" placeholder="Message Alpha..." autocomplete="off">
    <button onclick="sendMessage()">Send</button>
</div>

<script>
async function sendMessage() {
    const input = document.getElementById("message");
    const chat = document.getElementById("chat");
    const message = input.value.trim();

    if (!message) return;

    chat.innerHTML += `
        <div class="message user">${escapeHtml(message)}</div>
    `;

    input.value = "";

    const thinking = document.createElement("div");
    thinking.className = "message alpha";
    thinking.textContent = "Thinking...";
    chat.appendChild(thinking);

    try {
        const response = await fetch("/chat", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({message: message})
        });

        const data = await response.json();
        thinking.textContent = data.reply || data.error || "Something went wrong.";
    } catch (error) {
        thinking.textContent = "I couldn't connect to Alpha.";
    }

    window.scrollTo(0, document.body.scrollHeight);
}

function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
}

document.getElementById("message").addEventListener("keydown", function(event) {
    if (event.key === "Enter") {
        sendMessage();
    }
});
</script>

</body>
</html>
"""

@app.route("/")
def home():
    return render_template_string(HTML)


@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json()
    message = data.get("message", "").strip()

    if not message:
        return jsonify({"error": "Please enter a message."})

    if not OPENROUTER_API_KEY:
        return jsonify({
            "error": "OPENROUTER_API_KEY has not been configured."
        }), 500

    try:
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://project-alpha.app",
                "X-Title": "Project Alpha"
            },
            json={
                "model": "openrouter/free",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are Alpha, the user's personal AI assistant. "
                            "Be helpful, friendly, clear, and honest. "
                            "Help the user with questions, learning, coding, "
                            "ideas, writing, and everyday tasks."
                        )
                    },
                    {
                        "role": "user",
                        "content": message
                    }
                ]
            },
            timeout=60
        )

        result = response.json()

        if response.status_code != 200:
            return jsonify({
                "error": result.get("error", {}).get(
                    "message",
                    "The AI service returned an error."
                )
            }), response.status_code

        reply = result["choices"][0]["message"]["content"]

        return jsonify({"reply": reply})

    except Exception as error:
        return jsonify({"error": str(error)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
