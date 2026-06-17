# PAStrack (Provincial Assessor's Office Tracking System)

**PAStrack** (formerly LegalTrack) is a Django-based case management and document tracking system designed for Local Government Units (LGUs) and the Provincial Assessor's Office in Cebu Province, Philippines. It streamlines the submission, tracking, and processing of real property documents and transactions.

**Live Deployment:** [https://pastrack.onrender.com/](https://pastrack.onrender.com/)

---

## 📋 Table of Contents

- [About the Project](#about-the-project)
- [Key Features](#key-features)
- [User Roles](#user-roles)
- [Sample Accounts (For Testing)](#sample-accounts-for-testing)
- [Tech Stack](#tech-stack)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running the Application](#running-the-application)
- [Deployment](#deployment)
- [Testing](#testing)
- [Project Structure](#project-structure)
- [Troubleshooting](#troubleshooting)
- [Team 15 Members](#team-15-members)
- [License](#license)

---

## 🎯 About the Project

PAStrack digitizes and automates the workflow of real property case submissions from municipal LGUs to the Provincial Capitol. It provides:
- **For LGU Administrators**: A streamlined wizard interface to submit cases, drafts, and required documents.
- **For Capitol Staff**: Dedicated dashboards for Receiving, Examining, Tax Mapping, Approving, Numbering, and Releasing documents.
- **For Citizens**: A transparent public tracking portal to check transaction status securely.
- **For Super Admins**: Advanced analytics, user administration, and comprehensive audit logs.

### Problem It Solves

Previously, LGUs had to submit physical documents to the Capitol, leading to lost paperwork, difficulty tracking case status, and slow processing times. PAStrack centralizes this workflow, providing real-time updates, structured workflows, and automated communication.

---

## ✨ Key Features

- **Comprehensive Case Management** - Track documents from Draft and Submission to Final Release with detailed statuses (e.g., Under Examination, For Taxmapping, For Approval).
- **Sequential Document Numbering** - Strict database-level locking for robust Tax Declaration number sequence generation for each LGU.
- **Robust Role-Based Access Control** - Highly specialized roles and granular access controls for each step in the provincial assessor's workflow.
- **Audit Trails** - Extensive logging of user logins, case creation, status changes, assignments, and approvals.
- **Security-First Approach** - Configurable session timeouts, forced password changes, account lockouts on failed logins, and Argon2 password hashing.
- **Email Notifications** - Integrated Brevo API and SMTP configurations to send updates and activation links automatically.
- **Public Portal** - Check case progress without authentication by entering the unique Tracking ID.

---

## 👥 User Roles

| Role | Abbreviation | Responsibilities |
| --- | --- | --- |
| **Super Admin** | `super_admin` | System administration, user management, analytics, audit logs |
| **LGU Admin** | `lgu_admin` | Submit transactions/cases on behalf of their municipality, track submissions |
| **Receiver** | `capitol_receiving` | Receive incoming physical documents from LGUs, verify completeness |
| **Examiner** | `capitol_examiner` | Review case details, examine legal and technical documents, request revisions |
| **Tax Mapper** | `capitol_taxmapper` | Assess boundary and mapping specifics, verify spatial and property records |
| **Approver** | `capitol_approver` | Approve or return cases for corrections |
| **Numberer** | `capitol_numberer` | Assign official case numbers and Tax Declaration numbers |
| **Releaser** | `capitol_releaser` | Mark cases as released and ready for pickup/delivery |

---

## 🔑 Sample Accounts (For Testing)

You can use the following dummy accounts to log in and test the different roles in the system:

| Role | Username (Staff ID) | Password |
| --- | --- | --- |
| **Super Admin** | `26-ADM-0001` | `team15Admin-Trae_` |
| **LGU San Remigio** | `26-LGU-0001` | `Admin@1213_Casas03-` |
| **Capitol Receiver** | `26-REC-0001` | `CapitolR@0302_Fortaleza03-` |
| **Capitol Examiner** | `26-EXM-0001` | `CapitolE@1207_Yu04-` |
| **Capitol Examiner 2** | `26-EXM-0005` | `CapitolEx@m1n3r_ForTesting-` |
| **Capitol Approver** | `26-APR-0001` | `CapitolA@1030_Vestil-` |
| **Capitol Numberer** | `26-NUM-0001` | `CapitolN@1018_Rubio-` |
| **Capitol Tax Mapper** | `26-TAX-0001` | `CapitolTM@1018_Sumucad-` *(To be implemented)* |
| **Capitol Releaser** | `26-REL-0001` | `CapitolR@1231_Laviste-` |

---

## 🛠 Tech Stack

### Backend
- **Django (v5.2.8)** - Core web framework
- **Python (v3.11+)** - Programming language
- **Django REST Framework** - API endpoints
- **SQLite / PostgreSQL (Supabase)** - Database options

### Frontend
- **React (v18.3.1)** - Interactive components via Vite (`frontend/` folder)
- **Vite (v5.4.11)** - Frontend build tool
- **Tailwind CSS (v3.4.17)** - Utility-first CSS framework
- **Django Templates** - Server-rendered UI elements

### Infrastructure & Deployment
- **Render** - Main production hosting platform
- **Vercel** - Secondary/alternative hosting
- **Whitenoise** - Static file serving

---

## 📦 Prerequisites

Before you begin, ensure you have the following installed:
- **Git** - Version control
- **Python 3.11 or higher** - [Download Python](https://www.python.org/downloads/)
- **Node.js 18+** (Optional) - Only needed for React frontend development
- **PowerShell** (Windows) or Terminal (macOS/Linux)

---

## 🚀 Installation

### 1. Clone the Repository
```bash
git clone https://github.com/kylesumucad18/PASTrack.git
cd PASTrack
```

### 2. Create and Activate Virtual Environment
**Windows (PowerShell):**
```powershell
py -m venv venv
.\venv\Scripts\Activate.ps1
```
**macOS/Linux:**
```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Install Dependencies
```powershell
pip install --upgrade pip
pip install -r requirements.txt
```
*For development features (optional):*
```powershell
pip install -r requirements-dev.txt
```

### 4. Install Frontend Dependencies (Optional)
If working with React components:
```powershell
cd frontend
npm install
cd ..
```

---

## ⚙️ Configuration

### 1. Create Environment File
Create a `.env` file in the project root and add the following placeholder values. In production, replace these placeholders with your actual credentials.

```env
# Security
DJANGO_SECRET_KEY=your_secret_key_here
DJANGO_DEBUG=True
DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost,.vercel.app,.onrender.com

# Database Provider (Options: sqlite, supabase)
LEGALTRACK_DB_PROVIDER=supabase
DATABASE_URL=postgresql://USER:PASSWORD@HOST:PORT/DBNAME

# Email Configuration (Brevo/SMTP)
EMAIL_HOST=smtp-relay.brevo.com
EMAIL_PORT=587
EMAIL_HOST_USER=your_email_user@smtp-brevo.com
EMAIL_HOST_PASSWORD=your_email_password
EMAIL_USE_TLS=1
EMAIL_USE_SSL=0
DEFAULT_FROM_EMAIL=your_sender_email@gmail.com

# Feature Toggles
LEGALTRACK_SEND_EMAILS=1
LEGALTRACK_SEND_CASE_EMAILS=1
LEGALTRACK_SHOW_ACTIVATION_LINK=1

# ConvertAPI
CONVERTAPI_SECRET=your_convertapi_secret_key
```

### 2. Run Database Migrations
```powershell
py manage.py migrate
```

### 3. Create a Superuser Account
```powershell
py manage.py createsuperuser
```
Follow the prompts to create your Super Admin account.

---

## 🏃 Running the Application

### Start the Django Development Server
```powershell
py manage.py runserver
```
The application will be available at:
- **Main Application:** `http://127.0.0.1:8000/`
- **Admin Panel:** `http://127.0.0.1:8000/admin/`

### Start the Frontend Development Server (Optional)
If you are developing React components:
```powershell
cd frontend
npm run dev
```

---

## 🚀 Deployment

This project is configured for deployment on **Render** (via `render.yaml`) and **Vercel** (via `vercel.json`). 

**To deploy to Render:**
1. Push your code to GitHub.
2. Go to the [Render Dashboard](https://dashboard.render.com/) and create a new **Blueprint Instance**.
3. Connect your repository. Render will automatically detect the `render.yaml` file.
4. Render will provision the web service, run `pip install -r requirements.txt`, collect static files, and execute database migrations.
5. Ensure you have added the production `.env` variables in your Render environment settings.

*Helpful Deployment Scripts:*
- `deploy-render.bat` / `deploy-render.ps1`: Scripts included in the repository for pushing updates to Render manually.

---

## 🧪 Testing

### Run All Tests
```powershell
py manage.py test
```

### Run Tests with Coverage (if installed)
```powershell
coverage run --source='.' manage.py test
coverage report
coverage html
```
View the coverage report by opening `htmlcov/index.html` in a browser.

---

## 📁 Project Structure

```
PASTrack/
├── api/                 # Django REST Framework endpoints
├── core/                # Main business logic, database models, views, and middleware
├── frontend/            # React + Vite source code for interactive UI components
├── legaltrack/          # Django project configuration (settings, urls, wsgi, asgi)
├── media/               # User-uploaded files and case documents
├── scripts/             # Utility scripts for deployment and maintenance
├── staticfiles/         # Collected static assets for deployment
├── templates/           # Global and app-level HTML templates
├── manage.py            # Django CLI entrypoint
├── requirements.txt     # Python production dependencies
└── README.md            # This documentation file
```

---

## 🔧 Troubleshooting

### Database Changes Not Reflected
After modifying models, ensure you generate and run migrations:
```powershell
py manage.py makemigrations
py manage.py migrate
```

### Vercel / Render Deployment Issues
Ensure your environment variables (like `DJANGO_SECRET_KEY` and `DATABASE_URL`) are properly set in the respective cloud dashboards. If you are experiencing IPv6 resolution issues on Render or Vercel, the app includes fallback mechanisms configured in `settings.py`.

### Email Activation Not Working in Dev
In development mode (`DJANGO_DEBUG=true`), activation emails may be printed directly to your console rather than sent. For production, properly configure your SMTP settings (`EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`) in `.env`.

### Cannot Connect to Database (Supabase)
1. Verify that your `DATABASE_URL` is exact.
2. Check that your local IP address is allowed in the Supabase project settings if restricted.
3. For local troubleshooting, you can fallback to SQLite by setting `LEGALTRACK_ALLOW_SQLITE_FALLBACK=true`.

---

## 👨‍💻 Team 15 Members

- **Kursten Dane M. Casas**
- **Dale Christian C. Fortaleza**
- **Rainric Randy P. Yu**
- **Ross Mikhail A. Vestil**
- **Joseph Kyle R. Sumucad**

---

## 📄 License

This project is part of a university capstone project for the Province of Cebu. All rights reserved.
