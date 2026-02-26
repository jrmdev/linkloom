from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from app.auth import auth_bp
from app.extensions import db
from app.models import User


@auth_bp.route("/bootstrap", methods=["GET", "POST"])
def bootstrap_admin():
    if User.query.count() > 0:
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm_password") or ""

        if not username or not password:
            flash("Username and password are required.", "error")
        elif password != confirm:
            flash("Passwords do not match.", "error")
        else:
            admin = User(username=username, is_admin=True, is_active=True)
            admin.set_password(password)
            db.session.add(admin)
            db.session.commit()
            flash("Admin account created. Please sign in.", "success")
            return redirect(url_for("auth.login"))

    return render_template("bootstrap.html", app_name="LinkLoom")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("web.dashboard"))

    if User.query.count() == 0:
        return redirect(url_for("auth.bootstrap_admin"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        user = User.query.filter_by(username=username).first()
        if user and user.is_active and user.check_password(password):
            login_user(user)
            return redirect(url_for("web.dashboard"))
        flash("Invalid credentials.", "error")

    return render_template("login.html", app_name="LinkLoom")


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))
