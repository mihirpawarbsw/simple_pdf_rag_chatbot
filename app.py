
from dotenv import load_dotenv
from langchain_groq import ChatGroq
from langchain_huggingface import HuggingFaceEndpointEmbeddings
from langchain_core.prompts import ChatPromptTemplate
from langchain_community.document_loaders import PyMuPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_chroma import Chroma
from operator import itemgetter
import chromadb
import tempfile
import os
import uuid
import sqlite3
import json
from datetime import datetime
from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    redirect,
    url_for,
    session
)
# -----------------------------
# Load ENV
# -----------------------------

load_dotenv()

app = Flask(__name__)
app.secret_key = "super_secret_key_123"
# -----------------------------
# SQLite Setup
# -----------------------------

DB_NAME = "chat_history.db"
USERNAME = "admin"

PASSWORD = "admin123"

def init_db():

    conn = sqlite3.connect(DB_NAME)

    cursor = conn.cursor()

    cursor.execute("""

        CREATE TABLE IF NOT EXISTS chats (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            session_id TEXT,

            title TEXT,

            messages TEXT,

            created_at TEXT
        )

    """)

    conn.commit()

    conn.close()

init_db()

# -----------------------------
# Retriever Store
# -----------------------------

retriever_store = {}

# -----------------------------
# Configure Retriever
# -----------------------------

def configure_retriever(uploaded_files, session_id):

    docs = []

    temp_dir = tempfile.TemporaryDirectory()

    for file in uploaded_files:

        temp_filepath = os.path.join(
            temp_dir.name,
            file.filename
        )

        file.save(temp_filepath)

        loader = PyMuPDFLoader(temp_filepath)

        docs.extend(loader.load())

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=1500,
        chunk_overlap=200
    )

    chunks = splitter.split_documents(docs)

    embeddings = HuggingFaceEndpointEmbeddings(

        model="sentence-transformers/all-MiniLM-L6-v2",

        huggingfacehub_api_token=os.getenv(
            "HUGGINGFACEHUB_API_TOKEN"
        )
    )

    client = chromadb.PersistentClient(
        path="./chroma_db"
    )

    collection_name = f"collection_{session_id}"

    vectordb = Chroma.from_documents(
        documents=chunks,
        embedding=embeddings,
        client=client,
        collection_name=collection_name
    )

    retriever = vectordb.as_retriever(
        search_kwargs={"k": 3}
    )

    retriever_store[session_id] = retriever

# -----------------------------
# Format Docs
# -----------------------------

def format_docs(docs):

    return "\n\n".join(
        [d.page_content for d in docs]
    )

# -----------------------------
# Source Metadata
# -----------------------------

def get_source_metadata(documents):

    sources = []

    source_ids = []

    for d in documents:

        metadata = {

            "source":
                os.path.basename(
                    d.metadata["source"]
                ),

            "page":
                d.metadata["page"] + 1,

            "content":
                d.page_content[:200]
        }

        idx = (
            metadata["source"],
            metadata["page"]
        )

        if idx not in source_ids:

            source_ids.append(idx)

            sources.append(metadata)

    return sources

# -----------------------------
# Save Chat
# -----------------------------

def save_chat(session_id, title, messages):

    conn = sqlite3.connect(DB_NAME)

    cursor = conn.cursor()

    cursor.execute("""

        INSERT INTO chats
        (session_id, title, messages, created_at)

        VALUES (?, ?, ?, ?)

    """, (

        session_id,

        title,

        json.dumps(messages),

        datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    ))

    conn.commit()

    conn.close()

@app.route("/clear_chroma")
def clear_chroma():

    client = chromadb.PersistentClient(
        path="./chroma_db"
    )

    collections = client.list_collections()

    for col in collections:

        client.delete_collection(
            col.name
        )

    return "ChromaDB Cleared"

# -----------------------------
# Update Chat
# -----------------------------

def update_chat(session_id, messages):

    conn = sqlite3.connect(DB_NAME)

    cursor = conn.cursor()

    # -----------------------------------
    # Prepare Conversation Context
    # -----------------------------------

    conversation_text = ""

    for msg in messages:

        question = msg.get("question", "")

        answer = msg.get("answer", "")

        conversation_text += f"""

        User:
        {question}

        Assistant:
        {answer}

        """

    # -----------------------------------
    # Generate AI Chat Title
    # -----------------------------------

    try:

        title_prompt = f"""

        You are an AI assistant.

        Generate a SHORT,
        professional,
        concise title
        for this conversation.

        Rules:

        - Max 6 words
        - No quotes
        - No punctuation at end
        - Must summarize the topic
        - Similar to ChatGPT conversation titles

        Conversation:

        {conversation_text}

        Title:
        """

        title_response = llm.invoke(
            title_prompt
        )

        generated_title = (
            title_response.content
            .replace('"', '')
            .replace("\n", "")
            .strip()
        )

        # Fallback

        if len(generated_title) < 3:

            generated_title = "New Chat"

    except Exception as e:

        print("Title generation error:", e)

        generated_title = "New Chat"

    # -----------------------------------
    # Update Database
    # -----------------------------------

    cursor.execute("""

        UPDATE chats

        SET
            messages = ?,
            title = ?

        WHERE session_id = ?

    """, (

        json.dumps(messages),

        generated_title,

        session_id
    ))

    conn.commit()

    conn.close()

# -----------------------------
# Get Chats
# -----------------------------

def get_all_chats():

    conn = sqlite3.connect(DB_NAME)

    cursor = conn.cursor()

    cursor.execute("""

        SELECT session_id, title

        FROM chats

        ORDER BY id DESC

    """)

    rows = cursor.fetchall()

    conn.close()

    return rows

# -----------------------------
# Get Chat Messages
# -----------------------------

def get_chat_messages(session_id):

    conn = sqlite3.connect(DB_NAME)

    cursor = conn.cursor()

    cursor.execute("""

        SELECT messages

        FROM chats

        WHERE session_id = ?

    """, (session_id,))

    row = cursor.fetchone()

    conn.close()

    if row:

        return json.loads(row[0])

    return []

# -----------------------------
# LLM
# -----------------------------

llm = ChatGroq(

    groq_api_key=os.getenv("GROQ_API_KEY"),

    model_name="openai/gpt-oss-120b",

    temperature=0.1
)

# -----------------------------
# Prompt
# -----------------------------

qa_template = """

Use only the following context
to answer the question.

If you don't know,
say you don't know.

Context:
{context}

Question:
{question}

Answer:
"""

qa_prompt = ChatPromptTemplate.from_template(
    qa_template
)

# -----------------------------
# Home
# -----------------------------
# -----------------------------
# Login Required
# -----------------------------

def is_logged_in():

    return session.get("logged_in")

@app.route("/")
def index():

    if not is_logged_in():

        return redirect(
            url_for("login")
        )

    return render_template("index.html")

@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        username = request.form.get(
            "username"
        )

        password = request.form.get(
            "password"
        )

        if (

            username == USERNAME

            and

            password == PASSWORD
        ):

            session["logged_in"] = True

            return redirect(
                url_for("index")
            )

        return render_template(

            "login.html",

            error="Invalid credentials"
        )

    return render_template(
        "login.html"
    )

# -----------------------------
# Logout
# -----------------------------

@app.route("/logout")
def logout():

    session.clear()

    return render_template(
        "logout.html"
    )
# -----------------------------
# Upload
# -----------------------------

@app.route("/upload", methods=["POST"])
def upload_files():

    if not is_logged_in():

        return jsonify({

            "status":"error",

            "message":"Unauthorized"
        })

    uploaded_files = request.files.getlist(
        "files"
    )

    if not uploaded_files:

        return jsonify({

            "status":"error",

            "message":"No files uploaded"
        })

    session_id = str(uuid.uuid4())

    configure_retriever(
        uploaded_files,
        session_id
    )

    save_chat(
        session_id,
        "New Chat",
        []
    )

    return jsonify({

        "status":"success",

        "session_id":session_id
    })

# -----------------------------
# Ask Question
# -----------------------------

@app.route("/ask", methods=["POST"])
def ask_question():

    if not is_logged_in():

        return jsonify({

            "status":"error",

            "message":"Unauthorized"
        })

    data = request.json

    question = data.get("question")

    session_id = data.get("session_id")

    messages = data.get("messages", [])

    retriever = retriever_store.get(session_id)

    if retriever is None:

        return jsonify({

            "status":"error",

            "message":"Session expired"
        })

    retrieved_docs = retriever.invoke(question)

    chain = (

        {

            "context":
                itemgetter("question")
                | retriever
                | format_docs,

            "question":
                itemgetter("question")
        }

        | qa_prompt

        | llm
    )

    response = chain.invoke({

        "question":question
    })

    sources = get_source_metadata(
        retrieved_docs
    )

    messages.append({

        "question":question,

        "answer":response.content
    })

    update_chat(
        session_id,
        messages
    )

    return jsonify({

        "status":"success",

        "answer":response.content,

        "sources":sources
    })


# -----------------------------
# Get Chat List
# -----------------------------

@app.route("/get_chats")
def get_chats():

    if not is_logged_in():

        return jsonify({

            "status":"error",

            "message":"Unauthorized"
        })

    chats = get_all_chats()

    return jsonify(chats)

# -----------------------------
# Load Chat
# -----------------------------

@app.route("/load_chat/<session_id>")
def load_chat(session_id):

    if not is_logged_in():

        return jsonify({

            "status":"error",

            "message":"Unauthorized"
        })

    messages = get_chat_messages(
        session_id
    )

    return jsonify(messages)

# -----------------------------
# Main
# -----------------------------

if __name__ == "__main__":

    app.run(debug=True)