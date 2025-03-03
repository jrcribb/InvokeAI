from pathlib import Path
from typing import Optional

from invokeai.app.services.invoker import Invoker
from invokeai.app.services.shared.pagination import PaginatedResults
from invokeai.app.services.shared.sqlite.sqlite_common import SQLiteDirection
from invokeai.app.services.shared.sqlite.sqlite_database import SqliteDatabase
from invokeai.app.services.workflow_records.workflow_records_base import WorkflowRecordsStorageBase
from invokeai.app.services.workflow_records.workflow_records_common import (
    Workflow,
    WorkflowCategory,
    WorkflowNotFoundError,
    WorkflowRecordDTO,
    WorkflowRecordListItemDTO,
    WorkflowRecordListItemDTOValidator,
    WorkflowRecordOrderBy,
    WorkflowValidator,
    WorkflowWithoutID,
)
from invokeai.app.util.misc import uuid_string


class SqliteWorkflowRecordsStorage(WorkflowRecordsStorageBase):
    def __init__(self, db: SqliteDatabase) -> None:
        super().__init__()
        self._conn = db.conn

    def start(self, invoker: Invoker) -> None:
        self._invoker = invoker
        self._sync_default_workflows()

    def get(self, workflow_id: str) -> WorkflowRecordDTO:
        """Gets a workflow by ID. Updates the opened_at column."""
        cursor = self._conn.cursor()
        cursor.execute(
            """--sql
            UPDATE workflow_library
            SET opened_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'NOW')
            WHERE workflow_id = ?;
            """,
            (workflow_id,),
        )
        self._conn.commit()
        cursor.execute(
            """--sql
            SELECT workflow_id, workflow, name, created_at, updated_at, opened_at
            FROM workflow_library
            WHERE workflow_id = ?;
            """,
            (workflow_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise WorkflowNotFoundError(f"Workflow with id {workflow_id} not found")
        return WorkflowRecordDTO.from_dict(dict(row))

    def create(self, workflow: WorkflowWithoutID) -> WorkflowRecordDTO:
        try:
            # Only user workflows may be created by this method
            assert workflow.meta.category is WorkflowCategory.User
            workflow_with_id = Workflow(**workflow.model_dump(), id=uuid_string())
            cursor = self._conn.cursor()
            cursor.execute(
                """--sql
                INSERT OR IGNORE INTO workflow_library (
                    workflow_id,
                    workflow
                )
                VALUES (?, ?);
                """,
                (workflow_with_id.id, workflow_with_id.model_dump_json()),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return self.get(workflow_with_id.id)

    def update(self, workflow: Workflow) -> WorkflowRecordDTO:
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                """--sql
                UPDATE workflow_library
                SET workflow = ?
                WHERE workflow_id = ? AND category = 'user';
                """,
                (workflow.model_dump_json(), workflow.id),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return self.get(workflow.id)

    def delete(self, workflow_id: str) -> None:
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                """--sql
                DELETE from workflow_library
                WHERE workflow_id = ? AND category = 'user';
                """,
                (workflow_id,),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return None

    def get_many(
        self,
        order_by: WorkflowRecordOrderBy,
        direction: SQLiteDirection,
        category: WorkflowCategory,
        page: int = 0,
        per_page: Optional[int] = None,
        query: Optional[str] = None,
    ) -> PaginatedResults[WorkflowRecordListItemDTO]:
        # sanitize!
        assert order_by in WorkflowRecordOrderBy
        assert direction in SQLiteDirection
        assert category in WorkflowCategory
        count_query = "SELECT COUNT(*) FROM workflow_library WHERE category = ?"
        main_query = """
            SELECT
                workflow_id,
                category,
                name,
                description,
                created_at,
                updated_at,
                opened_at
            FROM workflow_library
            WHERE category = ?
            """
        main_params: list[int | str] = [category.value]
        count_params: list[int | str] = [category.value]

        stripped_query = query.strip() if query else None
        if stripped_query:
            wildcard_query = "%" + stripped_query + "%"
            main_query += " AND name LIKE ? OR description LIKE ? "
            count_query += " AND name LIKE ? OR description LIKE ?;"
            main_params.extend([wildcard_query, wildcard_query])
            count_params.extend([wildcard_query, wildcard_query])

        main_query += f" ORDER BY {order_by.value} {direction.value}"

        if per_page:
            main_query += " LIMIT ? OFFSET ?"
            main_params.extend([per_page, page * per_page])

        cursor = self._conn.cursor()
        cursor.execute(main_query, main_params)
        rows = cursor.fetchall()
        workflows = [WorkflowRecordListItemDTOValidator.validate_python(dict(row)) for row in rows]

        cursor.execute(count_query, count_params)
        total = cursor.fetchone()[0]

        if per_page:
            pages = total // per_page + (total % per_page > 0)
        else:
            pages = 1  # If no pagination, there is only one page

        return PaginatedResults(
            items=workflows,
            page=page,
            per_page=per_page if per_page else total,
            pages=pages,
            total=total,
        )

    def _sync_default_workflows(self) -> None:
        """Syncs default workflows to the database. Internal use only."""

        """
        An enhancement might be to only update workflows that have changed. This would require stable
        default workflow IDs, and properly incrementing the workflow version.

        It's much simpler to just replace them all with whichever workflows are in the directory.

        The downside is that the `updated_at` and `opened_at` timestamps for default workflows are
        meaningless, as they are overwritten every time the server starts.
        """

        try:
            cursor = self._conn.cursor()
            workflows_from_file: list[Workflow] = []
            workflows_to_update: list[Workflow] = []
            workflows_to_add: list[Workflow] = []
            workflows_dir = Path(__file__).parent / Path("default_workflows")
            workflow_paths = workflows_dir.glob("*.json")
            for path in workflow_paths:
                bytes_ = path.read_bytes()
                workflow_from_file = WorkflowValidator.validate_json(bytes_)

                assert workflow_from_file.id.startswith(
                    "default_"
                ), f'Invalid default workflow ID (must start with "default_"): {workflow_from_file.id}'

                assert (
                    workflow_from_file.meta.category is WorkflowCategory.Default
                ), f"Invalid default workflow category: {workflow_from_file.meta.category}"

                workflows_from_file.append(workflow_from_file)

                try:
                    workflow_from_db = self.get(workflow_from_file.id).workflow
                    if workflow_from_file != workflow_from_db:
                        self._invoker.services.logger.debug(
                            f"Updating library workflow {workflow_from_file.name} ({workflow_from_file.id})"
                        )
                        workflows_to_update.append(workflow_from_file)
                    continue
                except WorkflowNotFoundError:
                    self._invoker.services.logger.debug(
                        f"Adding missing default workflow {workflow_from_file.name} ({workflow_from_file.id})"
                    )
                    workflows_to_add.append(workflow_from_file)
                    continue

            library_workflows_from_db = self.get_many(
                order_by=WorkflowRecordOrderBy.Name,
                direction=SQLiteDirection.Ascending,
                category=WorkflowCategory.Default,
            ).items

            workflows_from_file_ids = [w.id for w in workflows_from_file]

            for w in library_workflows_from_db:
                if w.workflow_id not in workflows_from_file_ids:
                    self._invoker.services.logger.debug(
                        f"Deleting obsolete default workflow {w.name} ({w.workflow_id})"
                    )
                    # We cannot use the `delete` method here, as it only deletes non-default workflows
                    cursor.execute(
                        """--sql
                        DELETE from workflow_library
                        WHERE workflow_id = ?;
                        """,
                        (w.workflow_id,),
                    )

            for w in workflows_to_add:
                # We cannot use the `create` method here, as it only creates non-default workflows
                cursor.execute(
                    """--sql
                    INSERT INTO workflow_library (
                        workflow_id,
                        workflow
                    )
                    VALUES (?, ?);
                    """,
                    (w.id, w.model_dump_json()),
                )

            for w in workflows_to_update:
                # We cannot use the `update` method here, as it only updates non-default workflows
                cursor.execute(
                    """--sql
                    UPDATE workflow_library
                    SET workflow = ?
                    WHERE workflow_id = ?;
                    """,
                    (w.model_dump_json(), w.id),
                )

            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
