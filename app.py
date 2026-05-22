from dotenv import load_dotenv
from langchain_groq import ChatGroq
from langchain_huggingface import HuggingFaceEndpointEmbeddings
from langchain_core.prompts import ChatPromptTemplate
from langchain_community.document_loaders import PyMuPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_chroma import Chroma
from langchain_core.documents import Document
from operator import itemgetter
import chromadb
import tempfile
import os
import uuid
import sqlite3
import json
import re
from datetime import datetime
from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    redirect,
    url_for,
    session,
    Response
)

# -----------------------------
# Load ENV
# -----------------------------
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "super_secret_key_nexora_123")

DB_NAME = os.getenv("DB_NAME", "chat_history.db")
USERNAME = os.getenv("RAG_USERNAME", "admin")
PASSWORD = os.getenv("RAG_PASSWORD", "admin123")

# -----------------------------
# SQLite Setup
# -----------------------------
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT UNIQUE,
            title TEXT,
            messages TEXT,
            created_at TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS uploaded_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            filename TEXT,
            file_size INTEGER,
            uploaded_at TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS session_settings (
            session_id TEXT PRIMARY KEY,
            model_name TEXT,
            temperature REAL,
            system_prompt TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

# -----------------------------
# Retriever Store (In-memory fallback cache)
# -----------------------------
retriever_store = {}

# -----------------------------
# Session Settings Helpers
# -----------------------------
def get_session_settings(session_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT model_name, temperature, system_prompt FROM session_settings WHERE session_id = ?", (session_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {
            "model_name": row[0],
            "temperature": row[1],
            "system_prompt": row[2]
        }
    return {
        "model_name": "llama-3.3-70b-versatile",
        "temperature": 0.1,
        "system_prompt": "You are a professional enterprise AI assistant. Use the provided context to answer the user's question accurately."
    }

def save_session_settings(session_id, model_name, temperature, system_prompt):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO session_settings (session_id, model_name, temperature, system_prompt)
        VALUES (?, ?, ?, ?)
    """, (session_id, model_name, float(temperature), system_prompt))
    conn.commit()
    conn.close()

# -----------------------------
# LLM Loader
# -----------------------------
def get_llm(session_id):
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key or "your_groq_api_key" in api_key:
        raise ValueError("GROQ_API_KEY is not configured. Please add it to your .env file.")
    settings = get_session_settings(session_id)
    return ChatGroq(
        groq_api_key=api_key,
        model_name=settings["model_name"],
        temperature=settings["temperature"]
    )

# -----------------------------
# Embeddings Loader
# -----------------------------
def get_embeddings():
    hf_token = os.getenv("HUGGINGFACEHUB_API_TOKEN")
    if hf_token and "your_huggingface_token" not in hf_token:
        try:
            return HuggingFaceEndpointEmbeddings(
                model="sentence-transformers/all-MiniLM-L6-v2",
                huggingfacehub_api_token=hf_token
            )
        except Exception as e:
            print(f"Failed to create HuggingFaceEndpointEmbeddings: {e}. Falling back to local embeddings.")
            
    # Local fallback
    try:
        from langchain_huggingface import HuggingFaceEmbeddings
        return HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")
    except Exception as e:
        print(f"Failed to load local HuggingFaceEmbeddings: {e}")
        # Default fallback (last resort)
        return HuggingFaceEndpointEmbeddings(
            model="sentence-transformers/all-MiniLM-L6-v2",
            huggingfacehub_api_token=hf_token
        )

# -----------------------------
# Document Parser
# -----------------------------
def parse_document(filepath, filename):
    ext = os.path.splitext(filename)[1].lower()
    docs = []
    
    if ext == ".pdf":
        loader = PyMuPDFLoader(filepath)
        docs = loader.load()
    elif ext == ".docx":
        try:
            import docx
            doc = docx.Document(filepath)
            fullText = []
            for para in doc.paragraphs:
                fullText.append(para.text)
            text = "\n".join(fullText)
            docs = [Document(page_content=text, metadata={"source": filepath, "page": 0})]
        except Exception as e:
            print(f"Error reading docx: {e}")
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                docs = [Document(page_content=f.read(), metadata={"source": filepath, "page": 0})]
    elif ext == ".csv":
        try:
            import csv
            row_texts = []
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                reader = csv.DictReader(f)
                if reader.fieldnames:
                    for i, row in enumerate(reader):
                        row_desc = ", ".join([f"{k}: {v}" for k, v in row.items() if v])
                        row_texts.append(f"Row {i+1}: {row_desc}")
            text = "\n".join(row_texts)
            docs = [Document(page_content=text, metadata={"source": filepath, "page": 0})]
        except Exception as e:
            print(f"Error reading csv: {e}")
    else:
        # Default text loader (.txt, .md, .py, etc.)
        try:
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                docs = [Document(page_content=f.read(), metadata={"source": filepath, "page": 0})]
        except Exception as e:
            print(f"Error reading text file: {e}")

    # clean up metadata
    for doc in docs:
        doc.metadata["source"] = filename
        if "page" not in doc.metadata:
            doc.metadata["page"] = 0
            
    return docs

# -----------------------------
# Dynamic Retriever Getter
# -----------------------------
def get_retriever(session_id):
    if session_id in retriever_store:
        return retriever_store[session_id]
        
    try:
        client = chromadb.PersistentClient(path="./chroma_db")
        collections = client.list_collections()
        col_names = [col.name for col in collections]
        collection_name = f"collection_{session_id}"
        
        if collection_name in col_names:
            embeddings = get_embeddings()
            vectordb = Chroma(
                client=client,
                collection_name=collection_name,
                embedding_function=embeddings
            )
            retriever = vectordb.as_retriever(search_kwargs={"k": 4})
            retriever_store[session_id] = retriever
            return retriever
    except Exception as e:
        print(f"Error loading collection dynamically: {e}")
    return None

# -----------------------------
# Configure Retriever (Create DB)
# -----------------------------
def configure_retriever(uploaded_files, session_id):
    docs = []
    temp_dir = tempfile.TemporaryDirectory()
    
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    for file in uploaded_files:
        temp_filepath = os.path.join(temp_dir.name, file.filename)
        file.save(temp_filepath)
        
        # Save file metadata
        file_size = os.path.getsize(temp_filepath)
        cursor.execute("""
            INSERT INTO uploaded_files (session_id, filename, file_size, uploaded_at)
            VALUES (?, ?, ?, ?)
        """, (session_id, file.filename, file_size, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        
        file_docs = parse_document(temp_filepath, file.filename)
        docs.extend(file_docs)
        
    conn.commit()
    conn.close()
    
    if not docs:
        return None
        
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=1000,
        chunk_overlap=150
    )
    chunks = splitter.split_documents(docs)
    
    embeddings = get_embeddings()
    client = chromadb.PersistentClient(path="./chroma_db")
    collection_name = f"collection_{session_id}"
    
    vectordb = Chroma.from_documents(
        documents=chunks,
        embedding=embeddings,
        client=client,
        collection_name=collection_name
    )
    
    retriever = vectordb.as_retriever(search_kwargs={"k": 4})
    retriever_store[session_id] = retriever
    return retriever

# -----------------------------
# Format Docs
# -----------------------------
def format_docs(docs):
    return "\n\n".join([d.page_content for d in docs])

# -----------------------------
# Detect General Questions
# -----------------------------
def is_general_question(question):
    question = question.lower().strip()
    casual_patterns = [
        r"\bhi\b", r"\bhello\b", r"\bhey\b", r"\bbye\b",
        r"\bgood morning\b", r"\bgood evening\b", r"\bhow are you\b",
        r"\bwho are you\b", r"\bthank you\b", r"\bthanks\b",
        r"\bjoke\b", r"\bwhat is your name\b", r"\bhow old are you\b",
        r"\bwhat can you do\b", r"\bwhat is ai\b"
    ]
    for pattern in casual_patterns:
        if re.search(pattern, question):
            return True
    return False

# -----------------------------
# Source Metadata Formatter
# -----------------------------
def get_source_metadata(documents):
    sources = []
    source_ids = []
    for d in documents:
        metadata = {
            "source": d.metadata.get("source", "Unknown"),
            "page": d.metadata.get("page", 0) + 1,
            "content": d.page_content[:250]
        }
        idx = (metadata["source"], metadata["page"])
        if idx not in source_ids:
            source_ids.append(idx)
            sources.append(metadata)
    return sources

# -----------------------------
# Database Chat Helpers
# -----------------------------
def save_chat(session_id, title, messages):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO chats (session_id, title, messages, created_at)
        VALUES (?, ?, ?, ?)
    """, (
        session_id,
        title,
        json.dumps(messages),
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ))
    conn.commit()
    conn.close()

def update_chat(session_id, messages):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # Generate chat title if it is "New Chat" or currently empty
    cursor.execute("SELECT title FROM chats WHERE session_id = ?", (session_id,))
    row = cursor.fetchone()
    current_title = row[0] if row else "New Chat"
    
    generated_title = current_title
    if current_title == "New Chat" and len(messages) > 0:
        try:
            conversation_text = ""
            for msg in messages[:2]: # Use first two exchanges to make a title
                conversation_text += f"User: {msg.get('question','')}\nAssistant: {msg.get('answer','')}\n"
                
            title_prompt = f"""You are an AI assistant. Generate a SHORT, professional, concise title for this conversation based on the initial messages.
            
Rules:
- Max 5 words
- No quotes or punctuation
- Summarize the topic

Conversation:
{conversation_text}

Title:"""
            llm = get_llm(session_id)
            title_response = llm.invoke(title_prompt)
            generated_title = title_response.content.replace('"', '').replace("\n", "").strip()
            if len(generated_title) < 3:
                generated_title = "New Chat"
        except Exception as e:
            print("Title generation error:", e)
            generated_title = "New Chat"

    cursor.execute("""
        UPDATE chats
        SET messages = ?, title = ?
        WHERE session_id = ?
    """, (json.dumps(messages), generated_title, session_id))
    conn.commit()
    conn.close()

def get_all_chats():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT session_id, title, created_at
        FROM chats
        ORDER BY id DESC
    """)
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_chat_messages(session_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT messages FROM chats WHERE session_id = ?", (session_id,))
    row = cursor.fetchone()
    conn.close()
    if row and row[0]:
        return json.loads(row[0])
    return []

# -----------------------------
# Query Condensation Helper
# -----------------------------
def condense_question(session_id, question, chat_history):
    if not chat_history:
        return question
        
    try:
        llm = get_llm(session_id)
        history_str = ""
        for msg in chat_history[-3:]: # use last 3 turns
            history_str += f"User: {msg.get('question', '')}\nAssistant: {msg.get('answer', '')}\n"
            
        condense_prompt = f"""Given the following conversation history and a follow-up question, rephrase the follow-up question to be a standalone question, in its original language.
        
Conversation History:
{history_str}

Follow-up Question: {question}

Standalone Question (do not output any explanation, just the question itself):"""

        response = llm.invoke(condense_prompt)
        standalone = response.content.strip()
        if standalone:
            return standalone
    except Exception as e:
        print(f"Error condensing question: {e}")
        
    return question

# -----------------------------
# Routes
# -----------------------------
def is_logged_in():
    return session.get("logged_in")

@app.route("/")
def index():
    if not is_logged_in():
        return redirect(url_for("login"))
    return render_template("index.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        if username == USERNAME and password == PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("index"))
        return render_template("login.html", error="Invalid credentials")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return render_template("logout.html")

@app.route("/upload", methods=["POST"])
def upload_files():
    if not is_logged_in():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
        
    uploaded_files = request.files.getlist("files")
    if not uploaded_files or not uploaded_files[0].filename:
        return jsonify({"status": "error", "message": "No files selected"}), 400
        
    session_id = request.form.get("session_id")
    is_new = False
    
    if not session_id or session_id == "null" or session_id == "undefined":
        session_id = str(uuid.uuid4())
        is_new = True
        
    # Check if retriever exists to see if we are appending
    retriever = get_retriever(session_id)
    
    if retriever is None:
        # Create new retriever & vector DB
        configure_retriever(uploaded_files, session_id)
        if is_new:
            save_chat(session_id, "New Chat", [])
    else:
        # Appending documents to existing retriever
        # We parse, save file details to DB, and add to vector db
        docs = []
        temp_dir = tempfile.TemporaryDirectory()
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        for file in uploaded_files:
            temp_filepath = os.path.join(temp_dir.name, file.filename)
            file.save(temp_filepath)
            
            # Check if file already exists in session to prevent duplicates
            cursor.execute("SELECT id FROM uploaded_files WHERE session_id = ? AND filename = ?", (session_id, file.filename))
            existing = cursor.fetchone()
            if existing:
                continue
                
            file_size = os.path.getsize(temp_filepath)
            cursor.execute("""
                INSERT INTO uploaded_files (session_id, filename, file_size, uploaded_at)
                VALUES (?, ?, ?, ?)
            """, (session_id, file.filename, file_size, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            
            file_docs = parse_document(temp_filepath, file.filename)
            docs.extend(file_docs)
            
        conn.commit()
        conn.close()
        
        if docs:
            splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=150)
            chunks = splitter.split_documents(docs)
            
            # Add to Chroma VectorDB
            client = chromadb.PersistentClient(path="./chroma_db")
            collection_name = f"collection_{session_id}"
            vectordb = Chroma(
                client=client,
                collection_name=collection_name,
                embedding_function=get_embeddings()
            )
            vectordb.add_documents(chunks)
            
    return jsonify({
        "status": "success",
        "session_id": session_id
    })

# -----------------------------
# Streaming SSE Q&A Route
# -----------------------------
@app.route("/ask_stream", methods=["POST"])
def ask_stream():
    if not is_logged_in():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
        
    data = request.json or {}
    question = data.get("question")
    session_id = data.get("session_id")
    messages = data.get("messages", [])
    language = data.get("language", "English")
    response_type = data.get("response_type", "detailed")
    
    if not question or not session_id:
        return jsonify({"status": "error", "message": "Missing question or session_id"}), 400
        
    def generate():
        # Get retriever
        retriever = get_retriever(session_id)
        settings = get_session_settings(session_id)
        
        # Check if no docs uploaded and is general query
        if retriever is None:
            # Check if there are actually uploaded files for this session in SQL
            # If server restarted, we check database first
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM uploaded_files WHERE session_id = ?", (session_id,))
            has_files = cursor.fetchone()[0] > 0
            conn.close()
            
            if not has_files and is_general_question(question):
                # Pure casual interaction
                try:
                    llm = get_llm(session_id)
                    prompt = f"""
                    {settings['system_prompt']}

                    Respond in {language}.
                    Response style: {response_type}.

                    User: {question}

                    Assistant:
                    """
                    answer_text = ""
                    for chunk in llm.stream(prompt):
                        token = chunk.content
                        answer_text += token
                        yield f"data: {json.dumps({'token': token})}\n\n"
                    # save chat
                    messages.append({"question": question, "answer": answer_text})
                    update_chat(session_id, messages)
                    yield f"data: {json.dumps({'done': True, 'sources': []})}\n\n"
                except Exception as e:
                    yield f"data: {json.dumps({'error': str(e)})}\n\n"
                    yield f"data: {json.dumps({'done': True, 'sources': []})}\n\n"
                return
            else:
                yield f"data: {json.dumps({'error': 'No knowledge base loaded. Please upload documents first.'})}\n\n"
                yield f"data: {json.dumps({'done': True, 'sources': []})}\n\n"
                return
                
        # RAG Execution
        try:
            llm = get_llm(session_id)
            is_casual = is_general_question(question)
            
            if is_casual:
                # Bypass RAG for simple greeting
                prompt = f"""
                {settings['system_prompt']}

                Respond in {language}.
                Response style: {response_type}.

                User: {question}

                Assistant:
                """
                answer_text = ""
                for chunk in llm.stream(prompt):
                    token = chunk.content
                    answer_text += token
                    yield f"data: {json.dumps({'token': token})}\n\n"
                messages.append({"question": question, "answer": answer_text})
                update_chat(session_id, messages)
                yield f"data: {json.dumps({'done': True, 'sources': []})}\n\n"
                return
                
            # Full RAG with condensation and retrieval
            condensed_q = condense_question(session_id, question, messages)
            retrieved_docs = retriever.invoke(condensed_q)
            context_str = format_docs(retrieved_docs)
            sources = get_source_metadata(retrieved_docs)
            
            system_instructions = settings["system_prompt"]
            rag_prompt = f"""
            {system_instructions}

            You are an enterprise RAG AI assistant.

            IMPORTANT INSTRUCTIONS:
            - Respond ONLY in {language} language.
            - Response style should be {response_type}.
            - If response type is short, keep answer concise and direct.
            - If response type is detailed, explain properly with context and bullet points where useful.
            - Use ONLY the provided context.
            - If answer is not present in context, clearly say you do not know.

            Context:
            {context_str}

            Question:
            {question}

            Answer:
            """
            
            answer_text = ""
            for chunk in llm.stream(rag_prompt):
                token = chunk.content
                answer_text += token
                yield f"data: {json.dumps({'token': token})}\n\n"
                
            # Filter sources if the bot didn't know the answer
            unknown_patterns = ["i don't know", "do not know", "not available", "not mentioned", "cannot find", "no information"]
            if any(pat in answer_text.lower() for pat in unknown_patterns):
                final_sources = []
            else:
                final_sources = sources
                
            yield f"data: {json.dumps({'done': True, 'sources': final_sources})}\n\n"
            
            # Save the new message pair
            messages.append({"question": question, "answer": answer_text})
            update_chat(session_id, messages)
            
        except Exception as e:
            yield f"data: {json.dumps({'error': f'Generation error: {str(e)}'})}\n\n"
            yield f"data: {json.dumps({'done': True, 'sources': []})}\n\n"
            
    return Response(generate(), mimetype="text/event-stream")

# -----------------------------
# Document Management APIs
# -----------------------------
@app.route("/get_session_files/<session_id>")
def get_session_files(session_id):
    if not is_logged_in():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT filename, file_size, uploaded_at
        FROM uploaded_files
        WHERE session_id = ?
        ORDER BY id ASC
    """, (session_id,))
    rows = cursor.fetchall()
    conn.close()
    
    files = [{"filename": r[0], "file_size": r[1], "uploaded_at": r[2]} for r in rows]
    return jsonify(files)

@app.route("/delete_file", methods=["POST"])
def delete_file():
    if not is_logged_in():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
        
    data = request.json or {}
    session_id = data.get("session_id")
    filename = data.get("filename")
    
    if not session_id or not filename:
        return jsonify({"status": "error", "message": "Missing arguments"}), 400
        
    # Delete from database
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM uploaded_files WHERE session_id = ? AND filename = ?", (session_id, filename))
    conn.commit()
    conn.close()
    
    # Delete from vector DB
    try:
        client = chromadb.PersistentClient(path="./chroma_db")
        collection_name = f"collection_{session_id}"
        # Instantiate Chroma
        vectordb = Chroma(
            client=client,
            collection_name=collection_name,
            embedding_function=get_embeddings()
        )
        # Delete items matching source filename
        vectordb.delete(where={"source": filename})
        # If vector DB is now empty, delete collection and retriever
        cursor_check = sqlite3.connect(DB_NAME)
        c_check = cursor_check.cursor()
        c_check.execute("SELECT COUNT(*) FROM uploaded_files WHERE session_id = ?", (session_id,))
        count = c_check.fetchone()[0]
        cursor_check.close()
        
        if count == 0:
            client.delete_collection(collection_name)
            if session_id in retriever_store:
                del retriever_store[session_id]
        else:
            # Recreate retriever
            retriever_store[session_id] = vectordb.as_retriever(search_kwargs={"k": 4})
            
    except Exception as e:
        print(f"Error removing file from vector DB: {e}")
        
    return jsonify({"status": "success", "message": "File deleted successfully"})

# -----------------------------
# Settings APIs
# -----------------------------
@app.route("/get_settings/<session_id>")
def get_settings(session_id):
    if not is_logged_in():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    settings = get_session_settings(session_id)
    return jsonify(settings)

@app.route("/save_settings/<session_id>", methods=["POST"])
def save_settings_route(session_id):
    if not is_logged_in():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
        
    data = request.json or {}
    model_name = data.get("model_name", "llama-3.3-70b-versatile")
    temperature = data.get("temperature", 0.1)
    system_prompt = data.get("system_prompt", "You are a professional enterprise AI assistant.")
    
    save_session_settings(session_id, model_name, temperature, system_prompt)
    return jsonify({"status": "success", "message": "Settings updated"})

# -----------------------------
# Chat management
# -----------------------------
@app.route("/get_chats")
def get_chats():
    if not is_logged_in():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    chats = get_all_chats()
    # returns session_id, title, created_at
    return jsonify(chats)

@app.route("/load_chat/<session_id>")
def load_chat(session_id):
    if not is_logged_in():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    messages = get_chat_messages(session_id)
    return jsonify(messages)

@app.route("/rename_chat/<session_id>", methods=["POST"])
def rename_chat(session_id):
    if not is_logged_in():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    data = request.json or {}
    title = data.get("title")
    if not title:
        return jsonify({"status": "error", "message": "Title is required"}), 400
        
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE chats SET title = ? WHERE session_id = ?", (title, session_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "success", "message": "Chat renamed"})

@app.route("/delete_chat/<session_id>", methods=["POST"])
def delete_chat(session_id):
    if not is_logged_in():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
        
    # Delete from DB
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM chats WHERE session_id = ?", (session_id,))
    cursor.execute("DELETE FROM uploaded_files WHERE session_id = ?", (session_id,))
    cursor.execute("DELETE FROM session_settings WHERE session_id = ?", (session_id,))
    conn.commit()
    conn.close()
    
    # Delete collection
    try:
        client = chromadb.PersistentClient(path="./chroma_db")
        client.delete_collection(f"collection_{session_id}")
    except Exception as e:
        print(f"Chroma collection delete error: {e}")
        
    if session_id in retriever_store:
        del retriever_store[session_id]
        
    return jsonify({"status": "success", "message": "Chat deleted"})

@app.route("/clear_chroma")
def clear_chroma():
    if not is_logged_in():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    try:
        client = chromadb.PersistentClient(path="./chroma_db")
        collections = client.list_collections()
        for col in collections:
            client.delete_collection(col.name)
        retriever_store.clear()
        return "ChromaDB Cleared"
    except Exception as e:
        return f"Error clearing ChromaDB: {e}", 500

# -----------------------------
# Main
# -----------------------------
if __name__ == "__main__":
    app.run(debug=True)