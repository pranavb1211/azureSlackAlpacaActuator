# 🤖 Azure Slack → GPT → Alpaca MCP Actuator

## 🚀 Overview

The **Azure Slack-MCP Actuator** is a fully autonomous FastAPI service that listens to Slack events, interprets user messages using **Azure GPT-4o**, and then executes trading or data actions through an **Alpaca MCP server**.

It acts as a **bridge between Slack, Azure OpenAI, and Alpaca MCP**, enabling natural-language control of stock trading and portfolio management.

---

## 🧩 Architecture

Slack Message → FastAPI Listener → GPT Reasoning (Azure GPT-4o)
↓
MCP Client (HTTP)
↓
Executes command on Alpaca MCP
↓
Sends Result + Friendly Summary back to Slack

## Startup Command in azure

In App Service → Configuration → General Settings → Startup Command, set:

pip install -r requirements.txt && gunicorn -w 4 -k uvicorn.workers.UvicornWorker azureSlackActuator:app


