"""Editable starting points for new notebook pages."""

_EMPTY_TASK = '<li class="unchecked" style="-qt-paragraph-type:empty;"><br></li>'

PAGE_TEMPLATES = {
    "Study Notes": """
        <p><b>Topic</b></p>
        <p><br></p>
        <h3>Key ideas</h3>
        <ul><li><br></li></ul>
        <h3>Worked example</h3>
        <p><br></p>
        <h3>Questions to review</h3>
        <ul><li><br></li></ul>
        <h3>Summary</h3>
        <p><br></p>
    """,
    "Project Planner": """
        <p><b>Goal</b></p>
        <p><br></p>
        <h3>Milestones</h3>
        <table border="1" cellspacing="0" cellpadding="5">
            <tr><th>Milestone</th><th>Owner</th><th>Due</th></tr>
            <tr><td>&nbsp;</td><td>&nbsp;</td><td>&nbsp;</td></tr>
            <tr><td>&nbsp;</td><td>&nbsp;</td><td>&nbsp;</td></tr>
        </table>
        <h3>Dependencies and risks</h3>
        <ul><li><br></li></ul>
        <h3>Next actions</h3>
        <ul><li><br></li></ul>
    """,
    "Recipe": """
        <p><b>Servings</b> &nbsp;&nbsp;&nbsp; <b>Prep time</b> &nbsp;&nbsp;&nbsp; <b>Cook time</b></p>
        <p><br></p>
        <h3>Ingredients</h3>
        <ul><li><br></li><li><br></li></ul>
        <h3>Directions</h3>
        <ol><li><br></li><li><br></li></ol>
        <h3>Notes</h3>
        <p><br></p>
    """,
    "To-Do List": f"""
        <h3>Today</h3>
        <ul>{_EMPTY_TASK * 3}</ul>
        <h3>Upcoming</h3>
        <ul>{_EMPTY_TASK * 2}</ul>
        <h3>Waiting on</h3>
        <ul>{_EMPTY_TASK}</ul>
    """,
    "Meeting Notes": """
        <p><b>Date</b> &nbsp;&nbsp;&nbsp; <b>Attendees</b></p>
        <p><br></p>
        <h3>Agenda</h3>
        <ul><li><br></li></ul>
        <h3>Discussion and decisions</h3>
        <ul><li><br></li></ul>
        <h3>Action items</h3>
        <table border="1" cellspacing="0" cellpadding="5">
            <tr><th>Action</th><th>Owner</th><th>Due</th></tr>
            <tr><td>&nbsp;</td><td>&nbsp;</td><td>&nbsp;</td></tr>
        </table>
    """,
    "Experiment Log": """
        <p><b>Date</b> &nbsp;&nbsp;&nbsp; <b>Run ID</b></p>
        <p><br></p>
        <h3>Question and hypothesis</h3>
        <p><br></p>
        <h3>Setup and procedure</h3>
        <ol><li><br></li></ol>
        <h3>Measurements and observations</h3>
        <table border="1" cellspacing="0" cellpadding="5">
            <tr><th>Measurement</th><th>Value</th><th>Units</th></tr>
            <tr><td>&nbsp;</td><td>&nbsp;</td><td>&nbsp;</td></tr>
        </table>
        <h3>Conclusion and next run</h3>
        <p><br></p>
    """,
}
