from collections import defaultdict

from django.db.models import BooleanField, ExpressionWrapper, Q
from django.db.models.functions import Lower

from database.schedule_models import InterventionDate
from database.user_models import Participant, ParticipantFieldValue


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

    # InterventionDates and ParticipantFieldValue can be stuck into lookup dictionaries, to avoid
    # database calls in the inner loop.
    intervention_query = InterventionDate.objects.filter(participant__in=query)
    field_value_query = ParticipantFieldValue.objects.filter(participant__in=query)
    # don't convert these to regular dicts, in the case of no values it instantiates an empty list
    # inside the main loop, and then skips over it - otherwise we would get a KeyError.
    intervention_dates = defaultdict(list)
    field_values = defaultdict(list)
    # ordering by intervention / field name is sufficient, don't need to order by participant.
    for intervention in intervention_query.order_by(Lower('name')):
        intervention_dates[intervention.participant_id].append(intervention)
    for field_value in field_value_query.order_by(Lower('field_name')):
        field_values[field_value.participant_id].append(field_value)

    participants_data = []
    for participant in query:
        # Get the list of the basic columns, which are present in every study.
        participant_values = [getattr(participant, field) for field in basic_columns]
        # Convert the datetime object into a string in YYYY-MM-DD format
        participant_values[0] = participant_values[0].strftime('%Y-%m-%d')
        # Add all values for intervention dates
        for intervention_date in intervention_dates[participant.id]:
            if intervention_date.date is not None:
                participant_values.append(intervention_date.date.strftime('%Y-%m-%d'))
            else:
                participant_values.append(None)
        # Add all values for custom fields
        for custom_field_val in field_values[participant.id]:
            participant_values.append(custom_field_val.value)
        participants_data.append(participant_values)

    return participants_data
