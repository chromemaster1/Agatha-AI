"""
═══════════════════════════════════════════════════════════════════
MAIN.PY — 4 SURGICAL FIXES
═══════════════════════════════════════════════════════════════════

FIX 1  — Admin support reply crashes (phantom console code)
FIX 2  — User ticket persistence + notification on admin reply
FIX 3  — Home balance ≠ portfolio balance (unit sum vs USD sum)
FIX 4  — Delete closed/resolved ticket (new route)

HOW TO APPLY each fix is marked with ── FIND / REPLACE ──
═══════════════════════════════════════════════════════════════════
"""


# ══════════════════════════════════════════════════════════════════
#  FIX 1 + FIX 2 — api_admin_support_reply
#
#  BUG:  A leftover mock block sits between the guard checks and the
#        try block:
#
#          if selected_id in tickets:
#              print(...)
#              admin_reponse = input(...)   ← THIS CRASHES THE SERVER
#              print(...)
#          else:
#              print(...)
#
#        Flask raises a ValueError because you can't call input()
#        inside a web request — that's why admin reply always 500s.
#
#  FIND this entire function in main.py (search: def api_admin_support_reply)
#  REPLACE the whole function with this:
# ══════════════════════════════════════════════════════════════════

@app.route('/api/admin/support/ticket/<ticket_id>/reply', methods=['POST'])
@admin_required
def api_admin_support_reply(ticket_id):
    """
    Admin sends a reply to a user's support ticket.
    - Saves message with is_staff=True
    - Sets ticket status → in_progress
    - Creates a Notification for the user (lights up their bell)
    """
    admin   = get_current_user()
    data    = request.get_json(force=True) or {}
    message = (data.get('message') or '').strip()

    if not message:
        return jsonify({"error": "Message required"}), 400

    ticket = SupportTicket.query.filter_by(ticket_id=ticket_id).first()
    if not ticket:
        return jsonify({"error": "Ticket not found"}), 404

    try:
        msg = SupportMessage(
            ticket_id = ticket.id,
            user_id   = admin.id,
            message   = message,
            is_staff  = True,
        )
        db.session.add(msg)

        ticket.status     = 'in_progress'
        ticket.updated_at = datetime.utcnow()

        # ── Notify the user so their bell lights up ────────────────
        notif = Notification(
            user_id = ticket.user_id,
            title   = '💬 Support replied to your ticket',
            message = message[:120],
            type    = 'info',
        )
        db.session.add(notif)

        db.session.commit()

        log.info("Admin reply saved: ticket=%s admin=%s", ticket_id, admin.email)

        return jsonify({
            "success":    True,
            "message_id": msg.id,
            "ticket_id":  ticket_id,
        })

    except Exception as e:
        db.session.rollback()
        log.error("api_admin_support_reply failed: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════
#  FIX 2 (continued) — User's active ticket persistence
#
#  ADD this new route. It lets support.html reload the user's open
#  ticket on every page load / login, so the chat is never lost.
#
#  PASTE this anywhere in main.py after the @login_required imports.
# ══════════════════════════════════════════════════════════════════

@app.route('/api/support/my-tickets')
@login_required
def api_support_my_tickets():
    """
    GET /api/support/my-tickets
    Returns the current user's open / in_progress ticket (most recent),
    plus the last 30 messages so the UI can restore the chat on reload.

    Also returns all closed tickets as archived history.
    """
    user = get_current_user()

    try:
        # Active ticket — most recent open or in_progress
        active = (SupportTicket.query
                  .filter_by(user_id=user.id)
                  .filter(SupportTicket.status.in_(['open', 'in_progress']))
                  .order_by(SupportTicket.created_at.desc())
                  .first())

        active_data = None
        if active:
            msgs = (SupportMessage.query
                    .filter_by(ticket_id=active.id)
                    .order_by(SupportMessage.id.asc())
                    .limit(100)
                    .all())
            active_data = {
                "ticket_id":  active.ticket_id,
                "subject":    active.subject,
                "status":     active.status,
                "created_at": active.created_at.isoformat(),
                "messages": [{
                    "id":         m.id,
                    "message":    m.message,
                    "is_staff":   m.is_staff,
                    "created_at": m.created_at.isoformat(),
                } for m in msgs],
            }

        # Archived (closed) tickets
        closed = (SupportTicket.query
                  .filter_by(user_id=user.id, status='closed')
                  .order_by(SupportTicket.created_at.desc())
                  .limit(20)
                  .all())

        archived = [{
            "ticket_id":  t.ticket_id,
            "subject":    t.subject,
            "status":     t.status,
            "created_at": t.created_at.isoformat(),
            "closed_at":  t.closed_at.isoformat() if t.closed_at else None,
        } for t in closed]

        return jsonify({
            "active":   active_data,
            "archived": archived,
        })

    except Exception as e:
        log.error("api_support_my_tickets: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════
#  FIX 3 — Home balance vs Portfolio balance mismatch
#
#  BUG:  In the @app.route('/') home() function, line ~1528:
#
#    total_balance = sum((w.balance or 0.0) for w in user.wallets)
#
#  This sums raw crypto UNITS (1 BTC + 50 ETH + 1000 USDT = 1051).
#  But portfolio uses USD value from MarketData prices.
#  So home shows 1051 and portfolio shows $200,000+.
#
#  FIND this line in the home() route:
#    total_balance = sum((w.balance or 0.0) for w in user.wallets) if user.wallets else 0.0
#
#  REPLACE with this block:
# ══════════════════════════════════════════════════════════════════

    # Calculate total balance in USD — same logic as /api/wallet-summary
    # so home dashboard and portfolio always show the same number
    total_balance = 0.0
    if user.wallets:
        for w in user.wallets:
            if not w.balance:
                continue
            md = (MarketData.query
                  .filter_by(symbol=w.symbol.upper())
                  .order_by(MarketData.last_updated.desc())
                  .first())
            if md and md.price:
                total_balance += float(w.balance) * float(md.price)
            elif w.symbol.upper() in ('USDT', 'USDC', 'DAI', 'BUSD'):
                # stablecoins = 1:1 USD when no market data
                total_balance += float(w.balance)
            # else: unknown price, skip (don't add raw units)


# ══════════════════════════════════════════════════════════════════
#  FIX 4 — Admin delete closed/resolved ticket
#
#  ADD this new route. The admin JS will call it.
#  PASTE anywhere after api_admin_close_ticket in main.py.
# ══════════════════════════════════════════════════════════════════

@app.route('/api/admin/support/ticket/<ticket_id>/delete', methods=['POST'])
@admin_required
def api_admin_delete_ticket(ticket_id):
    """
    Admin deletes a closed/resolved support ticket and all its messages.
    Only tickets with status='closed' can be deleted.
    """
    ticket = SupportTicket.query.filter_by(ticket_id=ticket_id).first()
    if not ticket:
        return jsonify({"error": "Ticket not found"}), 404

    if ticket.status not in ('closed', 'resolved'):
        return jsonify({
            "error": "Only closed or resolved tickets can be deleted."
        }), 400

    try:
        # Delete all messages first (foreign key)
        SupportMessage.query.filter_by(ticket_id=ticket.id).delete()
        db.session.delete(ticket)
        db.session.commit()

        log.info("Admin deleted ticket %s", ticket_id)
        return jsonify({"success": True})

    except Exception as e:
        db.session.rollback()
        log.error("api_admin_delete_ticket failed: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════
#  FIX 4 (continued) — Also fix api_admin_close_ticket so it also
#  sends the user a notification when admin closes their ticket.
#
#  FIND def api_admin_close_ticket and REPLACE with this:
# ══════════════════════════════════════════════════════════════════

@app.route('/api/admin/support/ticket/<ticket_id>/close', methods=['POST'])
@admin_required
def api_admin_close_ticket(ticket_id):
    """Admin closes a support ticket and notifies the user."""
    ticket = SupportTicket.query.filter_by(ticket_id=ticket_id).first()
    if not ticket:
        return jsonify({"error": "Ticket not found"}), 404
    try:
        ticket.status    = 'closed'
        ticket.closed_at = datetime.utcnow()

        # Notify user their ticket was resolved
        notif = Notification(
            user_id = ticket.user_id,
            title   = '✅ Support ticket resolved',
            message = 'Your support ticket has been marked as resolved. Reply to reopen.',
            type    = 'success',
        )
        db.session.add(notif)
        db.session.commit()

        return jsonify({"success": True})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500
