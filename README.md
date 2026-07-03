# Nexora.AI — Transforming Documents Into Intelligent Conversations

Nexora.AI is a document-intelligence web application that lets users upload files (PDF, TXT, DOC/DOCX, PPT/PPTX), index them into a knowledge base, and "chat" with that knowledge base using natural language. Beyond chat, it bundles a suite of analysis tools — knowledge graphs, mind maps, clustering, NLP analytics, an action-item tracker, and one-click executive report generation — into a single workspace.

This README documents the two front-end views that make up the product (`login.html` and `index.html`) and the backend contract they expect, so the app can be understood, run, and extended.

---

## 1. Overview

| Page | Purpose |
|---|---|
| `login.html` | Public marketing/landing page with an embedded **Sign in** modal (also links to `/register`). Explains the product, shows feature highlights, and is the entry point for unauthenticated users. |
| `index.html` | The authenticated **chat workspace** — sidebar with knowledge base management, chat history, and a main panel for conversational Q&A plus the "Explore Features" tool suite. |

Both pages are **Jinja2 templates** (note the `{{ url_for('static', filename='...') }}` calls), meaning the backend is expected to be a Python web framework such as **Flask**, serving these as rendered templates rather than static HTML.

---

## 2. Tech Stack

**Frontend Technology**
- Vanilla HTML5 / CSS3 / JavaScript (no frontend framework/build step)
- [Marked.js](https://cdn.jsdelivr.net/npm/marked/marked.min.js) — Markdown rendering of AI responses
- [jsPDF](https://cdnjs.cloudflare.com/ajax/libs/jspdf/2.5.1/jspdf.umd.min.js) — client-side PDF export of conversations
- [Font Awesome 6.5.1](https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/) — iconography
- [SweetAlert2](https://cdn.jsdelivr.net/npm/sweetalert2@11) — modal alerts/confirmations
- Google Fonts: **Sora** (display) + **DM Sans** (body)
- HTML5 `<canvas>` — animated ambient "analytics" background (neural network / live bar chart visualization)
- Browser **Web Speech API** — voice input for chat

**Backend (implied by template/API contract — not included in these files)**
- Templating engine compatible with Jinja2 (Flask is the conventional choice)
- A document ingestion/indexing pipeline (likely RAG: chunking + embeddings + vector store)
- Server-Sent Events (SSE) for streaming chat responses
- Session-based auth (`POST` login form, `/register` route)

---

## 3. Project / Static File Structure:-

The templates reference the following static assets, implying this expected folder layout:

```
project-root/
├── templates/
│   ├── login.html          # Landing page + login modal
│   └── index.html          # Main chat workspace
├── static/
│   ├── css/
│   │   ├── style.css            # Core/shared styles, themes, layout
│   │   ├── full_report.css      # Executive Report modal styling
│   │   ├── action_tracker.css   # Action Tracker feature styling
│   │   ├── nlp_analytics.css    # NLP Analytics Suite styling
│   │   └── mindmap.css          # Mind Map feature styling
│   ├── js/
│   │   ├── graph.js             # Knowledge Graph (KnowledgeGraph.open())
│   │   ├── full_report.js       # Executive Report (openReportModal())
│   │   ├── nlp_analytics.js     # NLP Analytics (NLPAnalytics.open())
│   │   ├── action_tracker.js    # Action Tracker (ActionTracker.open())
│   │   ├── cluster.js           # Cluster Universe (ClusterUniverse.open())
│   │   └── mindmap.js           # Mind Map (MindMap.open())
│   └── images/
│       ├── logo_nexora_full.png
│       └── logo_nexora_icon.png
└── app.py (or equivalent)       # Backend routes (not provided)
```

> **Note:** the JS/CSS files above are referenced by `index.html` but were not included in the uploaded files. They must exist at those paths for the feature buttons (NLP Analytics, Mind Map, Knowledge Graph, Cluster Universe, Action Tracker, Executive Report) to function.

---

## 4. Pages in Detail

### 4.1 `login.html` — Landing Page

A single-page marketing site with smooth-scroll sections and an in-page login modal.

**Sections (anchored by `id`):**
| Section | Anchor | Content |
|---|---|---|
| Hero | — | Headline + CTA (`openLogin()`) |
| How it works | `#how` | 3-step explainer: *Ingest & index files → Select your analysis tool → Extract intelligence* |
| Features | `#features` | Chat with your files, Always see the source, See how things connect, Catch every to-do, One-click summaries, Private by default |
| Tools | `#tools` | The 7 in-app "superpowers": Knowledge Graph, Interactive Mind Map, Cluster Universe, Document Analytics, Smart Action Tracker, One-Click Report Builder, "The Engine Behind It All" |
| Use cases | `#usecases` | Contract review, personal finance, file search, faster reading/comprehension |
| FAQ | `#faq` | Common questions |
| Finale / CTA | — | "Get started free" → opens login modal |
| Footer | — | Brand, nav links, copyright |

**Login Modal**
- Triggered via `openLogin()` (button clicks, or visiting the page with `?login=true` / `#login` in the URL, or when the server renders an `error`/`success` flash message)
- Closed via `closeLogin()`, clicking the backdrop, or pressing `Escape`
- Plain HTML `<form method="POST">` with `username` and `password` fields — **submits directly to the backend** (no JS/AJAX interception), so the server is expected to handle the POST, validate credentials, and re-render the page with `{{ error }}` or `{{ success }}` Jinja variables on failure/success.
- Links to `/register` for new account creation.

**Background animation:** a Three.js-style particle/circle texture animation (`createCircleTexture`, `animate()`) renders an ambient visual in the hero area.

---

### 4.2 `index.html` — Chat Workspace

The authenticated application shell. Structure:

#### Sidebar
- **Collapse/expand toggle**
- **New Chat** button → `createNewChat()`
- **Knowledge Base** button → toggles the upload panel
- **Upload panel**
  - Drag/click file picker (accepts `.pdf .txt .doc .docx .ppt .pptx`, multiple files)
  - "Process Files" button → uploads & indexes selected files
  - Shows upload status and a live list of files indexed in the current session
- **Document selector** — checkboxes to scope a chat question to specific uploaded documents
- **Chat history** — list of past conversations, with rename/delete actions

#### Main Chat Area
- **Header** — brand/title, language selector, and the **"Explore Features"** dropdown exposing six tools:
  1. **NLP Analytics** — `NLPAnalytics.open()`
  2. **Mind Map** — `MindMap.open()`
  3. **Knowledge Graph** — `KnowledgeGraph.open()`
  4. **Cluster Universe** — `ClusterUniverse.open()`
  5. **Action Tracker** — `ActionTracker.open()`
  6. **Executive Report** — `openReportModal()`
- **Message stream** — user/bot bubbles, Markdown-rendered bot replies, typewriter-style streaming effect, and a **citation panel** ("Verified Sources") under each answer; clicking a source pill opens a **Citation Inspector** showing source document, page, and excerpt.
- **Composer** — text input (Enter to send), microphone button for speech-to-text input, and a "Download as PDF" action to export the full conversation client-side via jsPDF.
- **Theming** — multiple color themes (`aurora-gelato`, `cyan`, `lavender`, `obsidian`, `sherbet`, `sunset`, `tropical`) plus light/dark mode, persisted to `localStorage`.
- **Ambient background** — an animated `<canvas>` (`AnalyticsBackground` class) rendering a drifting neural-network/particle/bar-chart visualization behind the UI.

---

## 5. Backend API Contract

The frontend calls the following endpoints. The backend must implement these for the app to function (none of the implementations are included in the uploaded files — this is the contract `index.html` expects):

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/upload` | Upload one or more files (multipart `FormData`) to be processed/indexed into the knowledge base. Also reused with a JSON body (`{ session_id }`) as part of the settings-save flow. |
| `GET` | `/get_user_documents` | List all documents the user has previously indexed. |
| `POST` | `/delete_file` | Delete a specific uploaded/indexed file. |
| `GET` | `/get_session_files/<session_id>` | List files indexed within a specific chat session. |
| `GET` | `/get_settings/<session_id>` | Retrieve saved settings (e.g. language, response type) for a session. |
| `POST` | `/save_settings/<session_id>` | Persist session settings. |
| `POST` | `/ask_stream` | Submit a question for the AI to answer. Body: `{ question, session_id, messages, selected_docs, language, response_type }`. **Returns a Server-Sent Events (SSE) stream** of `data: {...}` lines, where each event may contain: `{ token }` (a streamed text chunk), `{ error }`, or `{ done: true, sources: [{ source, page, content }, ...] }` on completion. |
| `GET` | `/get_chats` | List the user's chat sessions/history (used to populate the sidebar). |
| `GET` | `/load_chat/<chatSessionId>` | Load all messages for a given chat session. |
| `POST` | `/rename_chat/<chatId>` | Rename a chat session. |
| `POST` | `/delete_chat/<chatId>` | Delete a chat session. |
| `POST` (form) | `/` (login.html) | Authenticate user from the login modal form (`username`, `password`). |
| — | `/register` | Account registration page (linked, not detailed in these files). |

**Streaming protocol detail (`/ask_stream`):** The client reads the response body as a stream, decodes UTF-8 chunks, splits on newlines, and parses any line beginning with `data: ` as JSON. This is a standard SSE-over-fetch pattern (not `EventSource`, since a `POST` with a JSON body is required).

---

## 6. Key Frontend Behaviors

- **Typewriter effect:** Bot responses are queued token-by-token (`typeQueue`) and rendered with a typing cursor animation (`startTypingEffect` / `typeLoop` / `flushTypingEffect`) for a "live generation" feel.
- **Citation system:** Every AI answer can carry a `sources` array; each source renders as a clickable pill, opening an inspector panel with the originating document, page number, and matched excerpt — supporting answer verifiability.
- **Document scoping:** Users can restrict a question to a subset of indexed documents via the sidebar checkboxes (`selected_docs`), enabling multi-document or single-document Q&A.
- **Session/chat model:** Each conversation has a `session_id` (client-generated UUID v4) and a separate persisted `chatId`/history record, allowing multiple parallel conversations with independent document scopes and settings.
- **Theming:** Theme and light/dark mode choices persist via `localStorage` and are reapplied on load (`window.onload`).
- **PDF export:** The full conversation can be exported client-side as a PDF using jsPDF — no server round-trip needed.
- **Voice input:** `startSpeechRecognition()` uses the browser's Web Speech API to transcribe speech into the question box.

---

## 7. The "Seven Superpowers" (Feature Suite)

As advertised on the landing page and accessible from the **Explore Features** menu in the workspace:

1. **Chat with your files** — core conversational Q&A over uploaded documents
2. **Knowledge Graph** — visualizes entities/relationships extracted from documents
3. **Interactive Mind Map** — hierarchical/visual breakdown of document concepts
4. **Cluster Universe** — groups related content/documents by similarity
5. **Document (NLP) Analytics** — statistical/linguistic analysis of the knowledge base
6. **Smart Action Tracker** — extracts action items and decisions from documents/conversations
7. **One-Click Report Builder** — generates an executive summary report from selected documents

Each is implemented in its own JS module (`graph.js`, `mindmap.js`, `cluster.js`, `nlp_analytics.js`, `action_tracker.js`, `full_report.js`) and opens inline within the chat workspace via `openFeatureInWorkspace(name, openFunc)`, replacing/augmenting the chat panel rather than navigating away.

---

## 8. Setup Notes (for the developer wiring up the backend)

To make these templates fully functional, the backend should:

1. Serve `login.html` at `/` (or equivalent) and handle its login `POST`, re-rendering with `error`/`success` template variables as needed.
2. Implement `/register` for account creation.
3. Serve `index.html` at an authenticated route (e.g. `/app`, `/chat`), guarding it behind a login-required check.
4. Implement all endpoints listed in [Section 5](#5-backend-api-contract), including the SSE streaming behavior for `/ask_stream`.
5. Provide the static assets listed in [Section 3](#3-project--static-file-structure) — particularly the six feature JS modules and their CSS, which are referenced but not included here.
6. Stand up a document ingestion pipeline (parsing PDF/DOC/PPT/TXT, chunking, embedding, storing) to back the upload/indexing and retrieval-augmented question answering.

---

## 9. License / Branding

"Nexora", "Nexora.AI", and associated logos are placeholders used throughout these templates (`© 2026 Nexora` in the footer). Replace branding assets in `static/images/` as needed.
