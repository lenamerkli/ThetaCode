```python
def get_db() -> SQLite_Connection:
    """
    Gets the database instance
    :return: a pointer to the database
    """
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite_connect('database.sqlite')
    return db


@app.teardown_appcontext
def close_connection(exception=None) -> None:  # noqa
    """
    destroys the database point
    :param exception: unused
    :return:
    """
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()


def query_db(query, args=(), one=False) -> list | tuple:
    """
    Runs a SQL query
    :param query: the query as a SQL statement
    :param args: arguments to be inserted into the query
    :param one: if this function should only return one result
    :return: the data from the database
    """
    conn = get_db()
    cur = conn.execute(query, args)
    result = cur.fetchall()
    conn.commit()
    cur.close()
    return (result[0] if result else None) if one else result


def relative_path(path: str) -> str:
    return str(join(dirname(__file__), path))


with app.app_context():
    with open(relative_path('resources/create_database.sql'), 'r') as f:
        _create_db = f.read()
    _conn = get_db()
    _conn.executescript(_create_db)
    _conn.commit()
    _conn.close()
```
