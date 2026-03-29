# FlareCloud Web App

Premium Minecraft Account Checker with Web Dashboard.

## 🚀 Cloud Deployment (Render.com)

1. **GitHub**: Create a **PRIVATE** repository at [github.com](https://github.com/new).
2. **Local Setup**:
   - Open terminal in this folder.
   - Run: `git init`
   - Run: `git add .`
   - Run: `git commit -m "Initial push"`
   - Run: `git branch -M main`
   - Run: `git remote add origin YOUR_REPO_URL`
   - Run: `git push -u origin main`
3. **Render**: 
   - Sign up at [render.com](https://render.com) with GitHub.
   - New **Web Service**.
   - Select your Flarecloud repo.
   - **Environment Variables**:
     - `ADMIN_KEY`: Your master password.
     - `FLASK_SECRET_KEY`: A random long string.
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn app:app`

## 💻 Local Usage
1. `pip install -r requirements.txt`
2. `python app.py`
