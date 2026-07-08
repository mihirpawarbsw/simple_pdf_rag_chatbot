# Nexora.AI — Transforming Documents Into Intelligent Conversations

Nexora.AI is an advanced enterprise document-intelligence web application that lets users upload multi-page corporate files (PDF, TXT, DOC/DOCX, PPT/PPTX), index them semantically, and "chat" with their knowledge base using natural language. Beyond conversational QA, it bundles a suite of **ten analytical superpowers** — knowledge graphs, interactive mind maps, coordinate clustering, timeline weaving, live web verification, and smart task trackers — into a single unified workspace.

---

## 1. Overview

| Page / Template | Purpose |
|---|---|
| `login.html` | Public marketing/landing page with an embedded **Sign In** modal and registration links. Highlights core product capabilities and FAQs. |
| `index.html` | Authenticated **Chat Workspace** featuring a document ingestion sidebar, chat history list, conversational Q&A pane, and dynamic workspace overlays for explore features. |

Both files are rendered as **Jinja2 templates** (e.g. using `{{ url_for('static', filename='...') }}`), indicating they are served dynamically by a Python framework such as **Flask**.

---

## 2. Tech Stack

**Frontend**
- **Core Layout**: Vanilla HTML5, CSS3 Custom Variables, and responsive Flexbox/Grid systems.
- **Glassmorphism Theme**: Translucent, blur-filtered containers (`backdrop-filter`) with glowing border highlights.
- **Markdown Processing**: [Marked.js](https://cdn.jsdelivr.net/npm/marked/marked.min.js) for markdown formatting of AI responses.
- **PDF Generation**: [jsPDF](https://cdnjs.cloudflare.com/ajax/libs/jspdf/2.5.1/jspdf.umd.min.js) for client-side chat transcript exports.
- **Iconography**: [Font Awesome 6.5.1](https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/) libraries.
- **Alerts & Modals**: [SweetAlert2](https://cdn.jsdelivr.net/npm/sweetalert2@11) for user notifications and deletion prompts.
- **Typography**: Google Fonts — **Sora** (headings) + **DM Sans** (body text).
- **Ambient Graphics**: An interactive HTML5 `<canvas>` background rendering a neural network node web.
- **Voice Ingestion**: Browser **Web Speech API** for voice-to-text input.

**Backend (Integration Layer)**
- **Routing & Templating**: Flask application server (`app.py` / `api_router.py`).
- **Semantic Vector Storage**: ChromaDB matching paragraph chunks using dense cosine similarity.
- **AI Orchestration**: LangChain connecting semantic lookups to LLM API endpoints (Groq / Gemini).
- **Web Grounding**: Async HTTP search queries (`web_augmentor.py` utilizing DuckDuckGo API) sourcing news feeds, tweets, and live articles.
- **Report Generation**: `python-docx` for compilations of DOCX executive reports.

---

## 3. Project File Structure

The project expects the following directory structure:

```
project-root/
├── templates/
│   ├── login.html             # Landing page + registration entry point
│   ├── index.html             # Core chat workspace layout
│   └── register.html          # Registration form
├── static/
│   ├── css/
│   │   ├── style.css          # Core styles, variables, light/dark mode, layouts
│   │   ├── full_report.css    # Executive Report configuration modal
│   │   ├── action_tracker.css # Kanban board Action Tracker layout
│   │   ├── nlp_analytics.css  # LexiScope suite panel layout
│   │   └── mindmap.css        # Radial mind map canvas layout
│   ├── js/
│   │   ├── action_tracker.js  # Kanban Action Item Tracker (ActionTracker.open())
│   │   ├── cluster.js         # Cluster Cloud Universe (ClusterUniverse.open())
│   │   ├── full_report.js     # Executive Document Report (openReportModal())
│   │   ├── graph.js           # D3 Knowledge Graph (KnowledgeGraph.open())
│   │   ├── mindmap.js         # Interactive Mind Map (MindMap.open())
│   │   ├── nlp_analytics.js   # LexiScope Suite NLP analysis (NLPAnalytics.open())
│   │   ├── pulse_grid.js      # StatSonar Reference Heatmaps (PulseGrid.open())
│   │   ├── timeline_weave.js  # Chronological Weaving (TimelineWeave.open())
│   │   ├── visual_pulse.js    # Visual Pulse Mood Board (VisualPulse.open())
│   │   └── web_augmentor.js   # TrendLens live verification (WebAugmentor.open())
│   └── images/
│       ├── logo_nexora_full.png      # Primary Nexora Brand Logo
│       └── logo_nexora_icon.png      # Secondary brand icon
└── app.py                     # Flask entry point and controller
```

---

## 4. The Ten Superpowers (Analytical Features)

Accessible directly from the **"Explore Features"** navigation dropdown in the main chat workspace:

1. **RAG Conversational Chat**
   The core QA companion. Users query their document indexes in plain English. Responses include citation badges linking directly to specific document paragraphs in the Source Context Inspector.
2. **Executive Report (One-Click Builder)**
   Compiles and exports multi-document summaries, key takeaways, and action items into a downloadable Word (.docx) or PDF document.
3. **Action Item & Decision Tracker**
   Scans your document corpus to automatically extract tasks, deadlines, owners, and decisions, displaying them in a Kanban board layout.
4. **TrendLens™ Live Verification**
   Compares document statements against live web news, articles, reviews, and tweets to verify accuracy. Claims are categorized as Confirmed, Contradicted, or Outdated.
5. **Knowledge Graph**
   Plots concept links and named entities across files in a dynamic, interactive D3.js force-directed network.
6. **Cluster Universe**
   Maps uploaded files in a 2D coordinates cloud based on semantic embedding similarity. Related files group together automatically.
7. **TimelineWeave**
   Weaves dated topics and arguments in documents chronologically alongside real-world milestones and news searched live from the web.
8. **Visual Pulse Mood Board**
   Pulls relevant live imagery from the web matching document keywords, compiling them into an interactive mood gallery.
9. **StatSonar (PulseGrid)**
   Extracts numerical data points and maps web credibility tiers (news, blogs, forum chatter) in a real-time credibility heatmap.
10. **LexiScope Suite**
    Advanced NLP engine that calculates emotional tone sentiment, builds word clouds, groups document concepts, extracts named entities, and rates readability.

---

## 5. UI Updates & Unifications

### 5.1 Centered "Generate Report" Button
All Explore workspaces have been unified to display **"Generate Report"** as the primary action button at the center of their idle state overlays. This establishes a clean user flow across different tools:
- **TimelineWeave**, **Visual Pulse**, **StatSonar**, **Mind Map**, **LexiScope**, and **Action Tracker** have all been modified in their respective JavaScript code blocks to display "Generate Report" when the workspace is loaded but inactive.

### 5.2 Chatbot Export PDF
The floating PDF export button located in the chat workspace has been redesigned:
- The text label "Export PDF" has been removed to reduce UI clutter.
- The button is now styled as a sleek, 36px square icon-only button with a central `fa-file-arrow-down` icon, matching the modern glassmorphism aesthetic.

---

## 6. Backend API Endpoints

The Flask server (`app.py`) handles the following endpoint routing:

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/upload` | Ingests multipart form files (PDF, DOCX, TXT) and indexes them. |
| `GET` | `/get_user_documents` | Retrieves list of all indexed documents for the logged-in session. |
| `POST` | `/delete_file` | Deletes a specific indexed document from the database. |
| `GET` | `/get_session_files/<session_id>` | Lists documents active within a specific conversation thread. |
| `GET` | `/get_settings/<session_id>` | Retrieves response length and style parameters. |
| `POST` | `/save_settings/<session_id>` | Persists session settings. |
| `POST` | `/ask_stream` | Receives queries and returns a Server-Sent Events (SSE) stream of token fragments. |
| `GET` | `/get_chats` | Returns list of historical conversations for the sidebar directory. |
| `GET` | `/load_chat/<chatSessionId>` | Loads message arrays for a historical conversation session. |
| `POST` | `/rename_chat/<chatId>` | Renames a chat thread. |
| `POST` | `/delete_chat/<chatId>` | Deletes a chat session history. |
| `POST` | `/cluster_universe` | Computes semantic coord values and summary labels for document clouds. |
| `POST` | `/mindmap` | Computes radial outline hierarchies from document corpuses. |

---

## 7. Configuration & Styling

- **Persisted Themes**: Color profiles are stored in browser `localStorage`. Themes include Lavender, Cyan, Sunset, Sherbet, Obsidian, and Tropical.
- **Response Styles**: Conversational responses can be toggled between detailed, standard, and concise.
- **Light/Dark Mode**: A stylesheet toggle swaps custom CSS variables to match ambient background illumination preference.
