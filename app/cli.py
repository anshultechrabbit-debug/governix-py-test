"""Operational commands.

    python -m app.cli create-master-admin --email admin@example.com --name "Platform Admin"
    python -m app.cli reembed [--organization ORG_ID]   (after changing the embedding model)
    python -m app.cli reset-two-step --email admin@example.com   (lost authenticator, no admin above to reset it)

The password is read from GOVERNIX_ADMIN_PASSWORD or prompted for; it is never
accepted as a command-line argument (shell history, process listings).
"""

import argparse
import getpass
import os
import sys

from app.core.config import get_settings
from app.core.database import create_db_engine, create_session_factory
from app.core.exceptions import AppError
from app.core.inputs import password_problems


def create_master_admin(args: argparse.Namespace) -> None:
    from app.core.model_registry import import_all_models
    from app.modules.users.service import create_master_admin as create

    import_all_models()
    password = os.environ.get("GOVERNIX_ADMIN_PASSWORD") or getpass.getpass("Password: ")
    problems = password_problems(password, email=args.email, name=args.name)
    if problems:
        sys.exit("The password must " + "; ".join(problems) + ".")
    engine = create_db_engine(get_settings())
    try:
        with create_session_factory(engine)() as session:
            user = create(session, args.email, args.name, password)
        print(f"Created master admin {user.email} ({user.id})")
    except AppError as exc:
        sys.exit(exc.message)
    finally:
        engine.dispose()


def reset_two_step(args: argparse.Namespace) -> None:
    """Break glass: turn off someone's two-step sign-in from the server (the Master Admin has nobody above them to
    reset it in the app). Signs them out everywhere, audits it and alerts them and their managers."""
    from datetime import UTC, datetime

    from sqlalchemy import func, select, update

    from app.core.model_registry import import_all_models
    from app.modules.audit.service import record_event
    from app.modules.auth import alerts
    from app.modules.auth.model import RefreshToken
    from app.modules.auth.service import clear_mfa
    from app.modules.users.model import User

    import_all_models()
    engine = create_db_engine(get_settings())
    try:
        with create_session_factory(engine)() as session:
            user = session.scalar(select(User).where(func.lower(User.email) == args.email.lower(),
                                                     User.deleted_at.is_(None)).with_for_update())
            if user is None:
                sys.exit(f"No account uses {args.email}.")
            if user.mfa_enabled_at is None and user.mfa_secret is None:
                sys.exit(f"Two-step sign-in is not on for {user.email}.")
            clear_mfa(session, user)
            user.token_version += 1
            session.execute(update(RefreshToken).where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
                            .values(revoked_at=datetime.now(UTC)))
            record_event(session, "user.mfa_reset", organization_id=user.organization_id, resource_type="user",
                         resource_id=user.id, details={"by": "server command line"})
            alerts.two_step_changed(session, user, "reset")
            session.commit()
        print(f"Two-step sign-in reset for {args.email}. They sign in with their password and can set it up again.")
    finally:
        engine.dispose()


def seed_db(args: argparse.Namespace) -> None:
    from seed import main as seed_main

    seed_main()


def reembed(args: argparse.Namespace) -> None:
    """Give chunks vectors from the configured embedding model (after changing EMBEDDING_PROVIDER or
    EMBEDDING_MODEL): only chunks whose vectors come from another model, through the embedding cache,
    so text this model has embedded before costs nothing."""
    import time

    from sqlalchemy import bindparam, select, update
    from sqlalchemy.dialects.postgresql import insert

    from app.core.model_registry import import_all_models
    from app.infrastructure.ai.embeddings.factory import create_embedder
    from app.modules.documents.model import Document, DocumentStatus
    from app.modules.ingestion.text import sha256_text
    from app.modules.search.model import Chunk, EmbeddingCache
    from app.workers.embeddings.tasks import embedding_input

    import_all_models()
    settings = get_settings()
    embedder = create_embedder(settings)
    if embedder is None:
        sys.exit("EMBEDDING_PROVIDER is none: there is nothing to embed with.")
    engine = create_db_engine(settings)
    done, started = 0, time.perf_counter()
    try:
        with create_session_factory(engine)() as session:
            documents = select(Document).where(Document.status == DocumentStatus.READY)
            if args.organization:
                documents = documents.where(Document.organization_id == args.organization)
            for document in session.scalars(documents).all():
                while True:
                    chunks = session.scalars(
                        select(Chunk).where(Chunk.document_id == document.id,
                                            (Chunk.embedding_model != embedder.model_id) | Chunk.embedding_model.is_(None))
                        .order_by(Chunk.chunk_index).limit(args.batch)
                    ).all()
                    if not chunks:
                        break
                    inputs = {c.id: embedding_input(document.title, c) for c in chunks}
                    hashes = {c.id: sha256_text(text) for c, text in ((c, inputs[c.id]) for c in chunks)}
                    cached = dict(session.execute(select(EmbeddingCache.text_hash, EmbeddingCache.embedding).where(
                        EmbeddingCache.model == embedder.model_id, EmbeddingCache.text_hash.in_(set(hashes.values())))).all())
                    missing = [c for c in chunks if hashes[c.id] not in cached]
                    if missing:
                        embedded = embedder.embed([inputs[c.id] for c in missing])
                        if embedded.model_id != embedder.model_id:
                            sys.exit(f"{embedder.model_id} is unavailable (got {embedded.model_id}); nothing more was changed.")
                        fresh = {hashes[c.id]: v for c, v in zip(missing, embedded.vectors, strict=True)}
                        session.execute(insert(EmbeddingCache).values(
                            [{"model": embedder.model_id, "text_hash": h, "embedding": v} for h, v in fresh.items()]
                        ).on_conflict_do_nothing())
                        cached.update(fresh)
                    session.connection().execute(
                        update(Chunk.__table__).where(Chunk.__table__.c.id == bindparam("chunk_id"))
                        .values(embedding=bindparam("vector"), embedding_model=bindparam("label")),
                        [{"chunk_id": c.id, "vector": cached[hashes[c.id]], "label": embedder.model_id} for c in chunks],
                    )
                    session.commit()
                    done += len(chunks)
                    print(f"{done} chunks re-embedded with {embedder.model_id} ({time.perf_counter() - started:.0f}s)", flush=True)
    finally:
        engine.dispose()
    print(f"Done: {done} chunks now use {embedder.model_id}.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    admin = commands.add_parser("create-master-admin", help="Bootstrap the platform administrator")
    admin.add_argument("--email", required=True)
    admin.add_argument("--name", required=True)
    admin.set_defaults(handler=create_master_admin)

    two_step = commands.add_parser("reset-two-step", help="Turn off an account's two-step sign-in (lost authenticator)")
    two_step.add_argument("--email", required=True)
    two_step.set_defaults(handler=reset_two_step)

    seed = commands.add_parser("seed", help="Seed the database with test organizations, branches, and users")
    seed.set_defaults(handler=seed_db)

    vectors = commands.add_parser("reembed", help="Re-embed chunks with the configured embedding model")
    vectors.add_argument("--organization", help="Only this organisation's documents (id)")
    vectors.add_argument("--batch", type=int, default=64)
    vectors.set_defaults(handler=reembed)

    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
