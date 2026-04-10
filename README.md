# 🗑️ Umoja Estate Bin Reporting System

A comprehensive web-based application built to help residents of Umoja Estate manage their waste collection. Residents can report full bins, view their collection schedules, and seamlessly pay for waste collection services using M-Pesa integration. 

## ✨ Features

* **Role-Based Access Control:** Separate dashboards and permissions for Residents, Managers, and Admins.
* **Bin Status Reporting:** Residents can flag their bins as "Full" or "Empty" and leave specific concerns.
* **Automated Billing:** Assigning a collection date automatically generates a KES 100 bill for the resident.
* **M-Pesa Integration:** Fully integrated with Safaricom's Daraja API for live STK Push payments. Auto-updates database balances via webhook callbacks.
* **Communication Hub:** Managers can send direct messages or broadcast announcements to residents.
* **Admin Controls:** Secure dashboard for adding, viewing, and removing system users.

## 🛠️ Tech Stack

* **Backend:** Python, Flask
* **Database:** MySQL (via `mysql-connector-python`)
* **Frontend:** HTML5, CSS3, Jinja2 Templating
* **Payments:** Safaricom Daraja API (Sandbox)
* **Webhooks:** Ngrok (for local M-Pesa callback testing)

## 🚀 Installation & Setup

If you want to run this project locally, follow these steps:

### 1. Prerequisites
* Python 3.11+
* MySQL Server (e.g., XAMPP, WAMP, or standalone MySQL)
* Git

### 2. Clone the Repository
```bash
git clone [https://github.com/philex4111/Binreporting-system.git](https://github.com/philex4111/Binreporting-system.git)
cd Binreporting-system