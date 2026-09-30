import os
import uuid
import sqlite3
import requests
from flask import Flask, request, jsonify, make_response, render_template_string

app = Flask(__name__)

DB_FILE = "alpha_memory.db"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
GROQ_TRANSCRIBE_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


def init_db():
    conn = sqlite3.connect(DB_FILE)
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
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(
        "SELECT memory FROM memories WHERE user_id = ? ORDER BY id ASC",
        (user_id,)
    ).fetchall()
    conn.close()
    return [row[0] for row in rows]


def save_memory(user_id, memory):
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "INSERT INTO memories (user_id, memory) VALUES (?, ?)",
        (user_id, memory)
    )
    conn.commit()
    conn.close()


init_db()

HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Project Alpha</title>
<style>
    * { box-sizing: border-box; }

    body {
        margin: 0;
        background: #11151b;
        color: #ffffff;
        font-family: Arial, sans-serif;
        height: 100vh;
        display: flex;
        flex-direction: column;
    }

    header {
        padding: 16px 18px;
        background: #181d25;
        border-bottom: 1px solid #2a303a;
        font-size: 20px;
        font-weight: 700;
    }

    #chat {
        flex: 1;
        overflow-y: auto;
        padding: 18px;
        display: flex;
        flex-direction: column;
        gap: 12px;
    }

    .message {
        max-width: 85%;
        padding: 12px 14px;
        border-radius: 16px;
        line-height: 1.45;
        white-space: pre-wrap;
        word-wrap: break-word;
    }

    .user {
        align-self: flex-end;
        background: #2b6cff;
    }

    .alpha {
        align-self: flex-start;
        background: #242a33;
    }

    .status {
        text-align: center;
        color: #aab2bf;
        font-size: 13px;
        min-height: 18px;
        padding: 0 12px 6px;
    }

    .composer {
        display: flex;
        gap: 8px;
        padding: 10px;
        background: #181d25;
        border-top: 1px solid #2a303a;
    }

    input {
        flex: 1;
        min-width: 0;
        border: 1px solid #343b46;
        background: #222832;
        color: white;
        border-radius: 12px;
        padding: 13px 14px;
        outline: none;
        font-size: 16px;
    }

    button {
        border: none;
        border-radius: 12px;
        color: white;
        font-size: 18px;
        min-width: 48px;
        padding: 0 14px;
        cursor: pointer;
    }

    #micButton {
        background: #303641;
    }

    #micButton.recording {
        background: #d22;
        animation: pulse 1s infinite;
    }

    #sendButton {
        background: #2b6cff;
    }

    button:disabled {
        opacity: 0.55;
        cursor: not-allowed;
    }

    @keyframes pulse {
        0% { transform: scale(1); }
        50% { transform: scale(1.06); }
        100% { transform: scale(1); }
    }
</style>
</head>

<body>
<header>🤖 Project Alpha</header>

<div id="chat">
    <div class="message alpha">Hello 👋 I'm Alpha. Type a message or tap 🎤 and talk to me.</div>
</div>

<div id="status" class="status"></div>

<div class="composer">
    <button id="micButton" onclick="toggleRecording()">🎤</button>
    <input id="message" placeholder="Talk to Alpha..." autocomplete="off">
    <button id="sendButton" onclick="sendMessage()">➤</button>
</div>

<script>
let mediaRecorder = null;
let audioChunks = [];
let isRecording = false;
let recordingMimeType = "";

const chat = document.getElementById("chat");
const messageInput = document.getElementById("message");
const micButton = document.getElementById("micButton");
const sendButton = document.getElementById("sendButton");
const statusBox = document.getElementById("status");

function addMessage(text, who) {
    const div = document.createElement("div");
    div.className = "message " + who;
    div.textContent = text;
    chat.appendChild(div);
    chat.scrollTop = chat.scrollHeight;
}

function setStatus(text) {
    statusBox.textContent = text || "";
}

function speak(text) {
    if (!("speechSynthesis" in window)) return;

    window.speechSynthesis.cancel();

    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = "en-US";
    utterance.rate = 1.0;
    utterance.pitch = 1.0;

    window.speechSynthesis.speak(utterance);
}

async function sendMessage(textFromVoice = null) {
    const text = (textFromVoice !== null ? textFromVoice : messageInput.value).trim();

    if (!text) return;

    addMessage(text, "user");
    messageInput.value = "";
    setStatus("Alpha is thinking...");
    sendButton.disabled = true;

    try {
        const response = await fetch("/chat", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({message: text})
        });

        const raw = await response.text();
        let data;

        try {
            data = JSON.parse(raw);
        } catch (parseError) {
            console.error("Server returned:", raw);
            throw new Error(
                "Alpha's server returned a non-JSON error (HTTP " +
                response.status +
                "). Please redeploy the latest main.py on Render."
            );
        }

        if (!response.ok) {
            throw new Error(data.error || "Chat request failed.");
        }

        addMessage(data.reply, "alpha");
        speak(data.reply);
        setStatus("");
    } catch (error) {
        addMessage("Sorry, something went wrong: " + error.message, "alpha");
        setStatus("");
    } finally {
        sendButton.disabled = false;
        messageInput.focus();
    }
}

async function toggleRecording() {
    if (isRecording) {
        stopRecording();
    } else {
        await startRecording();
    }
}

async function startRecording() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        alert("Your browser does not support microphone recording.");
        return;
    }

    try {
        const stream = await navigator.mediaDevices.getUserMedia({audio: true});

        let options = {};
        if (MediaRecorder.isTypeSupported("audio/webm;codecs=opus")) {
            options.mimeType = "audio/webm;codecs=opus";
        } else if (MediaRecorder.isTypeSupported("audio/webm")) {
            options.mimeType = "audio/webm";
        } else if (MediaRecorder.isTypeSupported("audio/mp4")) {
            options.mimeType = "audio/mp4";
        }

        mediaRecorder = new MediaRecorder(stream, options);
        recordingMimeType = mediaRecorder.mimeType || "audio/webm";
        audioChunks = [];

        mediaRecorder.ondataavailable = function(event) {
            if (event.data && event.data.size > 0) {
                audioChunks.push(event.data);
            }
        };

        mediaRecorder.onerror = function() {
            setStatus("Microphone recording failed.");
        };

        mediaRecorder.onstop = async function() {
            stream.getTracks().forEach(track => track.stop());

            const blob = new Blob(audioChunks, {type: recordingMimeType});
            audioChunks = [];

            if (blob.size === 0) {
                setStatus("");
                alert("No audio was recorded. Please try again.");
                return;
            }

            await transcribeAudio(blob);
        };

        mediaRecorder.start();
        isRecording = true;
        micButton.classList.add("recording");
        micButton.textContent = "⏹️";
        setStatus("🔴 Recording... tap the button again when you're done speaking.");
    } catch (error) {
        if (error.name === "NotAllowedError") {
            alert("Microphone permission was denied. Allow microphone access for Alpha in Chrome settings.");
        } else if (error.name === "NotFoundError") {
            alert("No microphone was found on this device.");
        } else {
            alert("Could not start the microphone: " + error.message);
        }
    }
}

function stopRecording() {
    if (!mediaRecorder || mediaRecorder.state === "inactive") return;

    isRecording = false;
    micButton.classList.remove("recording");
    micButton.textContent = "🎤";
    setStatus("⏳ Preparing your voice message...");
    mediaRecorder.stop();
}

async function transcribeAudio(blob) {
    setStatus("🧠 Converting your voice to text...");
    micButton.disabled = true;

    try {
        const formData = new FormData();

        let filename = "voice.webm";
        if (blob.type.includes("mp4")) {
            filename = "voice.mp4";
        }

        formData.append("audio", blob, filename);

        const response = await fetch("/transcribe", {
            method: "POST",
            body: formData
        });

        const data = await response.json();

        if (!response.ok) {
            throw new Error(data.error || "Transcription failed.");
        }

        const transcript = (data.text || "").trim();

        if (!transcript) {
            throw new Error("I could not detect any words. Please speak a little louder and try again.");
        }

        messageInput.value = transcript;
        setStatus("✅ I heard: " + transcript);

        await sendMessage(transcript);
    } catch (error) {
        setStatus("");
        alert("Voice error: " + error.message);
    } finally {
        micButton.disabled = false;
    }
}

messageInput.addEventListener("keydown", function(event) {
    if (event.key === "Enter") {
        event.preventDefault();
        sendMessage();
    }
});
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


@app.route("/transcribe", methods=["POST"])
def transcribe():
    groq_key = os.environ.get("GROQ_API_KEY")

    if not groq_key:
        return jsonify({
            "error": "GROQ_API_KEY is not set in Render Environment Variables."
        }), 500

    audio = request.files.get("audio")

    if not audio:
        return jsonify({"error": "No audio file was received."}), 400

    try:
        audio_bytes = audio.read()

        if not audio_bytes:
            return jsonify({"error": "The recorded audio was empty."}), 400

        filename = audio.filename or "voice.webm"
        content_type = audio.mimetype or "audio/webm"

        files = {
            "file": (filename, audio_bytes, content_type)
        }

        data = {
            "model": "whisper-large-v3-turbo",
            "response_format": "json"
        }

        result = requests.post(
            GROQ_TRANSCRIBE_URL,
            headers={
                "Authorization": f"Bearer {groq_key}"
            },
            files=files,
            data=data,
            timeout=60
        )

        if result.status_code != 200:
            try:
                error_data = result.json()
                error_message = error_data.get("error", {}).get("message", result.text)
            except Exception:
                error_message = result.text

            return jsonify({
                "error": f"Groq transcription error: {error_message}"
            }), 502

        result_data = result.json()
        text = result_data.get("text", "").strip()

        return jsonify({"text": text})

    except requests.Timeout:
        return jsonify({
            "error": "Voice transcription timed out. Please try again."
        }), 504

    except Exception as e:
        return jsonify({
            "error": f"Voice transcription failed: {str(e)}"
        }), 500


@app.route("/chat", methods=["POST"])
def chat():
    openrouter_key = os.environ.get("OPENROUTER_API_KEY")

    if not openrouter_key:
        return jsonify({
            "error": "OPENROUTER_API_KEY is not set."
        }), 500

    data = request.get_json(silent=True) or {}
    message = str(data.get("message", "")).strip()

    if not message:
        return jsonify({"error": "Message is empty."}), 400

    user_id = request.cookies.get("alpha_user_id")

    if not user_id:
        user_id = str(uuid.uuid4())

    lower_message = message.lower()

    if lower_message.startswith("remember that "):
        memory = message[len("remember that "):].strip()

        if memory:
            save_memory(user_id, memory)

        return jsonify({
            "reply": "Got it. I'll remember that for this browser."
        })

    memories = get_memories(user_id)

    system_prompt = """You are Alpha, a friendly personal AI assistant.
You are used by multiple people.
Each person has separate private memories.
Never reveal one person's memories to another person.
Be helpful, natural, and concise.
"""

    if memories:
        system_prompt += "\nPrivate memories for this user:\n"
        for memory in memories:
            system_prompt += f"- {memory}\n"

    payload = {
        "model": "openrouter/free:online",
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
    }

    try:
        result = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {openrouter_key}",
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=60
        )

        if result.status_code != 200:
            try:
                upstream = result.json()
                error_obj = upstream.get("error", {})
                detail = error_obj.get("message", result.text)
            except Exception:
                detail = result.text[:1000]

            return jsonify({
                "error": f"OpenRouter error (HTTP {result.status_code}): {detail}"
            }), 502

        try:
            result_data = result.json()
            choices = result_data.get("choices", [])
            if not choices:
                return jsonify({
                    "error": "OpenRouter returned no choices."
                }), 502

            reply = choices[0].get("message", {}).get("content")

            if not reply:
                return jsonify({
                    "error": "OpenRouter returned an empty reply."
                }), 502

        except Exception as e:
            return jsonify({
                "error": f"OpenRouter returned an unexpected response: {str(e)}"
            }), 502

        return jsonify({"reply": reply})

    except requests.Timeout:
        return jsonify({
            "error": "The AI request timed out. Please try again."
        }), 504

    except requests.RequestException as e:
        return jsonify({
            "error": f"Could not reach OpenRouter: {str(e)}"
        }), 502

    except Exception as e:
        return jsonify({
            "error": f"AI request failed: {str(e)}"
        }), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
