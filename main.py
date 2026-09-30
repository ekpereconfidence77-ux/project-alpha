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
            padding: 0 18px;
            background: #315efb;
            color: white;
            font-size: 16px;
        }

        #micButton {
            background: #e53935;
            min-width: 52px;
        }

        #micButton.listening {
            background: #35d07f;
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
    <button id="micButton" onclick="startVoice()">🎤</button>

    <input
        id="message"
        placeholder="Message Alpha..."
        autocomplete="off"
    >

    <button onclick="sendMessage()">Send</button>
</div>


<script>

let recognition = null;

function startVoice() {

    const SpeechRecognition =
        window.SpeechRecognition ||
        window.webkitSpeechRecognition;

    if (!SpeechRecognition) {
        alert(
            "Voice input is not supported by this browser. " +
            "Try opening Alpha in Google Chrome."
        );
        return;
    }

    if (recognition) {
        recognition.stop();
        recognition = null;
        return;
    }

    recognition = new SpeechRecognition();

    recognition.lang = "en-US";
    recognition.interimResults = false;
    recognition.continuous = false;

    const micButton = document.getElementById("micButton");

    recognition.onstart = function() {
        micButton.classList.add("listening");
        micButton.textContent = "🔴";
    };

    recognition.onresult = function(event) {

        const text =
            event.results[0][0].transcript;

        document.getElementById("message").value = text;

        sendMessage();
    };

    recognition.onerror = function(event) {

        console.log("Voice error:", event.error);

        micButton.classList.remove("listening");
        micButton.textContent = "🎤";

        recognition = null;
    };

    recognition.onend = function() {

        micButton.classList.remove("listening");
        micButton.textContent = "🎤";

        recognition = null;
    };

    recognition.start();
}


async function sendMessage() {

    const input =
        document.getElementById("message");

    const chat =
        document.getElementById("chat");

    const message =
        input.value.trim();

    if (!message) return;

    chat.innerHTML += `
        <div class="message user">
            ${escapeHtml(message)}
        </div>
    `;

    input.value = "";

    const thinking =
        document.createElement("div");

    thinking.className = "message alpha";
    thinking.textContent = "Thinking...";

    chat.appendChild(thinking);

    window.scrollTo(
        0,
        document.body.scrollHeight
    );

    try {

        const response = await fetch("/chat", {

            method: "POST",

            headers: {
                "Content-Type": "application/json"
            },

           
