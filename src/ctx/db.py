"""ChromaDB connection and query helpers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import chromadb
from chromadb.config import Settings

from ctx.config import get_config
from ctx.models import Document, Source

if TYPE_CHECKING:
    from chromadb import ClientAPI
    from chromadb.api.models.Collection import Collection

logger = logging.getLogger(__name__)

COLLECTION_NAME = "work_context"


class Database:
    """ChromaDB database connection manager."""

    _instance: Database | None = None

    def __init__(self) -> None:
        self._client: ClientAPI | None = None
        self._collection: Collection | None = None

    @classmethod
    def get_instance(cls) -> Database:
        """Get the singleton database instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton instance (for testing)."""
        cls._instance = None

    @property
    def client(self) -> ClientAPI:
        """Get the ChromaDB client, creating it if needed."""
        if self._client is None:
            config = get_config()
            db_path = config.database.path
            db_path.mkdir(parents=True, exist_ok=True)

            self._client = chromadb.PersistentClient(
                path=str(db_path),
                settings=Settings(
                    anonymized_telemetry=False,
                ),
            )
            logger.info("Connected to ChromaDB at %s", db_path)
        return self._client

    @property
    def collection(self) -> Collection:
        """Get the work_context collection, creating it if needed."""
        if self._collection is None:
            self._collection = self.client.get_or_create_collection(
                name=COLLECTION_NAME,
                metadata={"description": "Aggregated work context from multiple sources"},
            )
            logger.info(
                "Using collection '%s' with %d documents",
                COLLECTION_NAME,
                self._collection.count(),
            )
        return self._collection


def get_db() -> Database:
    """Get the database instance."""
    return Database.get_instance()


def reset_db() -> None:
    """Reset the database instance (for testing)."""
    Database.reset()


def add_documents(documents: list[Document]) -> int:
    """Add documents to the collection.

    Args:
        documents: List of Document objects to add.

    Returns:
        Number of documents added.
    """
    if not documents:
        return 0

    collection = get_db().collection

    ids = [doc.id for doc in documents]
    contents = [doc.content for doc in documents]
    metadatas = [doc.metadata.to_chroma_metadata() for doc in documents]

    collection.upsert(
        ids=ids,
        documents=contents,
        metadatas=metadatas,
    )

    logger.info("Upserted %d documents to collection", len(documents))
    return len(documents)


def delete_by_source(source: Source) -> int:
    """Delete all documents from a specific source.

    Args:
        source: The source to delete documents from.

    Returns:
        Number of documents deleted.
    """
    collection = get_db().collection

    before_count = collection.count()
    collection.delete(where={"source": source.value})
    after_count = collection.count()

    deleted = before_count - after_count
    logger.info("Deleted %d documents from source '%s'", deleted, source.value)
    return deleted


def search(
    query: str,
    *,
    n_results: int = 10,
    source: Source | list[Source] | None = None,
    since_timestamp: int | None = None,
    involvement: str | None = None,
    extra_filters: dict[str, str] | None = None,
) -> list[dict]:
    """Search the collection with optional filters.

    Args:
        query: The search query text.
        n_results: Maximum number of results to return.
        source: Filter by source(s).
        since_timestamp: Only return documents after this Unix timestamp.
        involvement: Filter by involvement type.
        extra_filters: Additional metadata filters as key=value pairs.

    Returns:
        List of result dicts with id, content, metadata, and distance.
    """
    collection = get_db().collection
    where = _build_where_clause(
        source=source,
        since_timestamp=since_timestamp,
        involvement=involvement,
        extra_filters=extra_filters,
    )

    results = collection.query(
        query_texts=[query],
        n_results=n_results,
        where=where,
        include=["documents", "metadatas", "distances"],
    )

    formatted: list[dict] = []
    if results["ids"] and results["ids"][0]:
        for i, doc_id in enumerate(results["ids"][0]):
            formatted.append(
                {
                    "id": doc_id,
                    "content": results["documents"][0][i] if results["documents"] else None,
                    "metadata": results["metadatas"][0][i] if results["metadatas"] else None,
                    "distance": results["distances"][0][i] if results["distances"] else None,
                }
            )

    return formatted


def keyword_search(
    keyword: str,
    *,
    n_results: int = 10,
    source: Source | list[Source] | None = None,
    since_timestamp: int | None = None,
) -> list[dict]:
    """Search by keyword (full-text, no embeddings).

    Args:
        keyword: The keyword to search for.
        n_results: Maximum number of results to return.
        source: Filter by source(s).
        since_timestamp: Only return documents after this Unix timestamp.

    Returns:
        List of result dicts with id, content, and metadata.
    """
    collection = get_db().collection
    where = _build_where_clause(source=source, since_timestamp=since_timestamp)

    results = collection.get(
        where=where,
        where_document={"$contains": keyword},
        limit=n_results,
        include=["documents", "metadatas"],
    )

    formatted: list[dict] = []
    if results["ids"]:
        for i, doc_id in enumerate(results["ids"]):
            formatted.append(
                {
                    "id": doc_id,
                    "content": results["documents"][i] if results["documents"] else None,
                    "metadata": results["metadatas"][i] if results["metadatas"] else None,
                }
            )

    return formatted


def get_stats() -> dict:
    """Get collection statistics.

    Returns:
        Dict with total count and counts by source.
    """
    collection = get_db().collection
    total = collection.count()

    by_source: dict[str, int] = {}
    for src in Source:
        results = collection.get(
            where={"source": src.value},
            include=[],
        )
        by_source[src.value] = len(results["ids"]) if results["ids"] else 0

    return {
        "total": total,
        "by_source": by_source,
    }


def get_document_by_id(doc_id: str) -> dict | None:
    """Get a single document by ID.

    Args:
        doc_id: The document ID.

    Returns:
        Dict with id, content, and metadata, or None if not found.
    """
    collection = get_db().collection

    results = collection.get(
        ids=[doc_id],
        include=["documents", "metadatas"],
    )

    if not results["ids"]:
        return None

    return {
        "id": results["ids"][0],
        "content": results["documents"][0] if results["documents"] else None,
        "metadata": results["metadatas"][0] if results["metadatas"] else None,
    }


def get_documents_by_ids(doc_ids: list[str]) -> list[dict]:
    """Get multiple documents by IDs.

    Args:
        doc_ids: List of document IDs.

    Returns:
        List of dicts with id, content, and metadata. Missing IDs are omitted.
    """
    if not doc_ids:
        return []

    collection = get_db().collection

    results = collection.get(
        ids=doc_ids,
        include=["documents", "metadatas"],
    )

    formatted: list[dict] = []
    if results["ids"]:
        for i, doc_id in enumerate(results["ids"]):
            formatted.append(
                {
                    "id": doc_id,
                    "content": results["documents"][i] if results["documents"] else None,
                    "metadata": results["metadatas"][i] if results["metadatas"] else None,
                }
            )

    return formatted


def list_documents(
    *,
    n_results: int = 10,
    source: Source | list[Source] | None = None,
    since_timestamp: int | None = None,
    extra_filters: dict[str, str] | None = None,
) -> list[dict]:
    """List documents with optional filters (no search query).

    Args:
        n_results: Maximum number of results to return.
        source: Filter by source(s).
        since_timestamp: Only return documents after this Unix timestamp.
        extra_filters: Additional metadata filters as key=value pairs.

    Returns:
        List of result dicts with id, content, and metadata.
    """
    collection = get_db().collection
    where = _build_where_clause(
        source=source,
        since_timestamp=since_timestamp,
        extra_filters=extra_filters,
    )

    results = collection.get(
        where=where,
        limit=n_results,
        include=["documents", "metadatas"],
    )

    formatted: list[dict] = []
    if results["ids"]:
        for i, doc_id in enumerate(results["ids"]):
            formatted.append(
                {
                    "id": doc_id,
                    "content": results["documents"][i] if results["documents"] else None,
                    "metadata": results["metadatas"][i] if results["metadatas"] else None,
                }
            )

    return formatted


def _build_where_clause(
    *,
    source: Source | list[Source] | None = None,
    since_timestamp: int | None = None,
    involvement: str | None = None,
    extra_filters: dict[str, str] | None = None,
) -> dict | None:
    """Build a ChromaDB where clause from filter parameters."""
    where_clauses: list[dict] = []

    if source is not None:
        if isinstance(source, list):
            where_clauses.append({"source": {"$in": [s.value for s in source]}})
        else:
            where_clauses.append({"source": source.value})

    if since_timestamp is not None:
        where_clauses.append({"timestamp": {"$gte": since_timestamp}})

    if involvement is not None:
        where_clauses.append({"my_involvement": involvement})

    if extra_filters:
        for key, value in extra_filters.items():
            where_clauses.append({key: value})

    if len(where_clauses) == 0:
        return None
    if len(where_clauses) == 1:
        return where_clauses[0]
    return {"$and": where_clauses}
