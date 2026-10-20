@echo off
rem Abre la app de reorganización de stock en el navegador (http://localhost:8501)
cd /d "%~dp0"
python -m streamlit run app.py
pause
