from airflow.sdk import dag, task
from datetime import datetime


@dag(
    dag_id="etl_pipeline",
    schedule="@daily",
    start_date=datetime(2023, 1, 1),
    catchup=False,
)
def etl_pipeline():
    @task
    def extract():
        """Simulate date extraction"""
        data = {"orders": [100, 200, 300]}
        print("Data extracted")
        return data

    @task
    def transform(data):
        """Simulate data transformation"""
        transformed_data = {"total_orders": sum(data["orders"])}
        print("Data transformed")
        return transformed_data

    @task
    def load(data):
        """Simulate loading data to a destination"""
        print(f"Data loaded: {data}")

    # Define task dependancies
    raw_data = extract()
    processed_data = transform(raw_data)
    load(processed_data)


etl_dag = etl_pipeline()
