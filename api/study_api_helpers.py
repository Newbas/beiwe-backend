from django.db.models import BooleanField, ExpressionWrapper, Q
from django.db.models.functions import Lower
from database.user_models import Participant


def get_values_for_view_study_table(
    study_id: int,
    start: int,
    length: int,
    sort_by_column_index: int,
    sort_in_descending_order: bool,
    contains_string: bool,
):
    basic_columns = ['created_on', 'patient_id', 'registered', 'os_type']
    sort_by_column = basic_columns[sort_by_column_index]
    if sort_in_descending_order:
        sort_by_column = '-' + sort_by_column
    query = (
        Participant.filtered_participants_for_study(study_id, contains_string)
        .order_by(sort_by_column)
        .annotate(registered=ExpressionWrapper(~Q(device_id__exact=''), output_field=BooleanField()))
        [start: start + length]
    )

    participants_data = []
    for participant in query:
        participant_values = []
        # Get the list of the basic columns, which are present in every study
        [participant_values.append(getattr(participant, field)) for field in basic_columns]
        # Convert the datetime object into a string in YYYY-MM-DD format
        participant_values[0] = participant_values[0].strftime('%Y-%m-%d')
        # Add all values for intervention dates
        for intervention_date in participant.intervention_dates.order_by(Lower('intervention__name')):
            if intervention_date.date is not None:
                participant_values.append(intervention_date.date.strftime('%Y-%m-%d'))
            else:
                participant_values.append(None)
        # Add all values for custom fields
        for custom_field_val in participant.field_values.order_by(Lower('field__field_name')):
            participant_values.append(custom_field_val.value)
        participants_data.append(participant_values)

    return participants_data
