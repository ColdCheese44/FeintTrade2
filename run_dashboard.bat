@echo off
cd /d C:\Users\brend\FeintTrade2
start "FeintTrade Dashboard Browser" /B python scripts\browser.py open http://localhost:8501 --wait-url http://localhost:8501/_stcore/health --timeout 90
streamlit run dashboard.py --server.headless=true --server.port=8501
