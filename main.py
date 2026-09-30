import os
import sqlite3
import uuid
import requests

from flask import Flask, request, jsonify, render_template_string, make_response

app = Flask(__name__)

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
DATABASE = "alpha_memory.db"


def init_db():
    conn = sqlite3.connect(DATABASE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            memory TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def get_memories(user_id):
    conn = sqlite3.connect(DATABASE)
    rows = conn.execute(
        "SELECT memory FROM memories WHERE user_id = ? ORDER BY id DESC LIMIT 20",
        (user_id,)
    ).fetchall()
    conn.close()
    return [row[0] for row in rows]


def save_memory(user_id, memory):
    conn = sqlite3.connect(DATABASE)
    conn.execute(
        "INSERT INTO memories (user_id, memory) VALUES (?, ?)",
        (user_id, memory)
    )
    conn.commit()
    conn.close()


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
            padding-bottom: 90px;
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

        thinking.textContent =
            data.reply || data.error || "Something went wrong.";

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


document.getElementById("message").addEventListener(
    "keydown",
    function(event) {
        if (event.key === "Enter") {
            sendMessage();
        }
    }
);
</script>

</body>
</html>
"""


@app.route("/")
def home():

    user_id = request.cookies.get("alpha_user_id")

    if not user_id:
        user_id = str(uuid.uuid4())

    response = make_response(render_template_string(HTML))

    response.set_cookie(
        "alpha_user_id",
        user_id,
        max_age=60 * 60 * 24 * 365,
        httponly=True,
        samesite="Lax"
    )

    return response


@app.route("/chat", methods=["POST"])
def chat():

    if not OPENROUTER_API_KEY:
        return jsonify({
            "error": "Alpha is not configured correctly."
        }), 500

    user_id = request.cookies.get("alpha_user_id")

    if not user_id:
        user_id = str(uuid.uuid4())

    data = request.get_json()
    message = data.get("message", "").strip()

    if not message:
        return jsonify({
            "error": "Please enter a message."
        })

    memories = get_memories(user_id)

    memory_text = ""

    if memories:
        memory_text = (
            "\n\nThings this user previously asked Alpha to remember:\n"
            + "\n".join("- " + m for m in memories)
        )

    system_prompt = """
You are Alpha, a friendly personal AI assistant.

You are being used by multiple people.

Each person has their own private conversation memory.
Never reveal another user's information or memory.

If a user explicitly tells you to remember something about them,
you may store it.

Be helpful, friendly, clear and honest.
""" + memory_text

    try:

        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",

            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://project-alpha.onrender.com",
                "X-Title": "Project Alpha"
            },

            json={
                "model": "openrouter/free",

                "messages": [
                    {
                        "role": "system",
                        "content": system_prompt
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

            error_message = (
                result.get("error", {}).get(
                    "message",
                    "The AI service returned an error."
                )
            )

            return jsonify({
                "error": error_message
            }), response.status_code

        reply = result["choices"][0]["message"]["content"]

        # Save simple explicit memory requests.
        lower_message = message.lower()

        if lower_message.startswith("remember that "):

            memory = message[13:].strip()

            if memory:
                save_memory(user_id, memory)

        return jsonify({
            "reply": reply
        })

    except Exception as error:

        return jsonify({
            "error": str(error)
        }), 500


init_db()


if __name__ == "__main__":

    port = int(os.environ.get("PORT", 5000))

    app.run(
        host="0.0.0.0",
        port=port
    )
