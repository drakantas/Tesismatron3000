from typing import Generator, Union
from datetime import datetime, timedelta
from asyncpg.pool import PoolConnectionHolder
from aiohttp.web import View, json_response, HTTPUnauthorized

from utils.map import map_users, parse_data_key
from utils.helpers import view, flatten, pass_user, permission_required, school_term_to_str, schedule_to_str


class StudentsList(View):
    @view('attendance.list')
    @permission_required('ver_listado_alumnos')
    async def get(self, user: dict):
        if 'school_term' in self.request.match_info:
            school_term = await self.get_school_term(user['escuela'], int(self.request.match_info['school_term']))
        else:
            school_term = await self.get_school_term(user['escuela'])

        if school_term is None:
            return {'school_term_has_not_begun': 'El ciclo académico no ha comenzado, tus acciones son limitadas'}

        # Estudiantes de escuela y tal
        students = await self.get_students(school_term['id'], user['escuela'])
        students = map_users(students)

        # Notas disponibles para este ciclo
        dd_grades = await self.get_grades(school_term['id'])

        # Ciclos académicos
        school_terms = await self.get_school_terms(user)

        current_school_term_id = school_term['id']

        can_do_grading = school_term['fecha_comienzo'] <= datetime.utcnow() <= school_term['fecha_fin']

        return {'students': students,
                'dd_grades': dd_grades,
                'school_terms': school_terms,
                'current_school_term_id': current_school_term_id,
                'can_do_grading': can_do_grading}

    async def get_school_terms(self, user: dict):

        def _g(stl: list) -> Generator:
            for st in stl:
                yield st['id'], school_term_to_str(st)

        return list(_g(await self._get_school_terms(user['escuela'])))

    async def _get_school_terms(self, school: int):
        query = '''
            SELECT id, fecha_comienzo, fecha_fin
            FROM ciclo_academico
            WHERE ciclo_academico.escuela = $1
            ORDER BY id DESC
            LIMIT 10
        '''
        async with self.request.app.db.acquire() as connection:
            return await (await connection.prepare(query)).fetch(school)

    async def get_grades(self, school_term: int):
        query = '''
            SELECT estructura_notas.nota_id as id, nota.descripcion
            FROM estructura_notas
            LEFT JOIN nota
                   ON nota.id = estructura_notas.nota_id
            WHERE estructura_notas.ciclo_acad_id = $1
            ORDER BY estructura_notas.nota_id ASC
        '''
        async with self.request.app.db.acquire() as connection:
            return await (await connection.prepare(query)).fetch(school_term)

    async def get_school_term(self, school: int, school_term: int = None):
        if school_term is None:
            query = '''
                SELECT id, fecha_comienzo, fecha_fin
                FROM ciclo_academico
                WHERE $1 >= fecha_comienzo AND
                      $1 <= fecha_fin AND
                      escuela = $2
                LIMIT 1
            '''
        else:
            query = '''
                SELECT id, fecha_comienzo, fecha_fin
                FROM ciclo_academico
                WHERE id = $1 AND
                      escuela = $2
                LIMIT 1
            '''

        async with self.request.app.db.acquire() as connection:
            statement = await connection.prepare(query)
            if school_term is None:
                return await statement.fetchrow(datetime.utcnow(), school)
            else:
                return await statement.fetchrow(school_term, school)

    async def get_students(self, school_term: int, school: int, student_role_id: int = 1, danger: int = 63,
                           amount: int = 1000):
        query = '''
            WITH alumno AS (
                SELECT usuario.id, tipo_documento, nombres, apellidos, escuela,
                    COALESCE(
                        (SELECT CAST(COUNT(CASE WHEN asistio=true THEN 1 ELSE NULL END) / CAST(COUNT(*) AS FLOAT) * 100
                        AS INT)
                            FROM asistencia
                            RIGHT JOIN ciclo_academico
                                    ON ciclo_academico.fecha_comienzo <= asistencia.fecha_registro AND
                                       ciclo_academico.fecha_fin >= asistencia.fecha_registro
                       WHERE asistencia.alumno_id = usuario.id
                       HAVING COUNT(*) >= 1),
                    0) AS asistencia,
                    proyecto.id as id_proyecto, COALESCE(proyecto.titulo, 'No registrado') as titulo_proyecto,
                    (
                        SELECT COUNT(true)
                        FROM integrante_proyecto
                        WHERE integrante_proyecto.proyecto_id = proyecto.id AND
                              integrante_proyecto.aceptado = true
                        LIMIT 1
                    ) as integrantes_proyecto
                FROM usuario
                LEFT JOIN integrante_proyecto
                        ON integrante_proyecto.usuario_id = usuario.id AND
                           integrante_proyecto.aceptado = true
                LEFT JOIN proyecto
                        ON proyecto.id = integrante_proyecto.proyecto_id
                INNER JOIN matricula
                        ON matricula.estudiante_id = usuario.id AND
                           matricula.ciclo_acad_id = $1
                WHERE rol_id = $2 AND
                      escuela = $3 AND
                      nombres != '' AND
                      apellidos != '' AND
                      autorizado = TRUE
                LIMIT $4
            )
            SELECT id, tipo_documento, nombres, apellidos, asistencia, escuela,
                   CASE WHEN asistencia < $5 THEN true ELSE false END as peligro,
                   id_proyecto, titulo_proyecto,
                   CASE WHEN integrantes_proyecto < 2 THEN true ELSE false END as proyecto_solo
            FROM alumno
            ORDER BY apellidos ASC
        '''

        async with self.request.app.db.acquire() as connection:
            stmt = await connection.prepare(query)
            return await stmt.fetch(school_term, student_role_id, school, amount, danger)


class ReadAttendanceReport(View):
    @pass_user
    async def get(self, user: dict):
        # Validar permisos...
        if not user['permissions']['ver_reportes_personales'] and self.request.match_info['student_id'] == 'my-own':
            raise HTTPUnauthorized
        elif user['permissions']['ver_reportes_personales'] and self.request.match_info['student_id'] != 'my-own':
            raise HTTPUnauthorized
        elif not user['permissions']['ver_listado_alumnos'] and not user['permissions']['ver_reportes_personales']:
            raise HTTPUnauthorized

        if self.request.match_info['student_id'] == 'my-own':
            student_id = user['id']
        else:
            student_id = int(self.request.match_info['student_id'])

        if 'school_term_id' in self.request.match_info:
            school_term_id = int(self.request.match_info['school_term_id'])
            school_term = await self.school_term_exists(school_term_id, user['escuela'], self.request.app.db)

            if school_term:
                school_term = {'id': school_term_id}
                del school_term_id
            else:
                # No se encontró ciclo académico
                return json_response({'message': 'Ciclo académico no encontrado'}, status=400)
        else:
            school_term = await self.fetch_school_term(user['escuela'], self.request.app.db)

            if not school_term:
                # No hay ciclo académico registrado para esta fecha
                return json_response({'message': 'No se encontró un ciclo académico para esta fecha'}, status=400)

        schedules = await self.fetch_schedules(school_term['id'], self.request.app.db)

        if not schedules:
            return json_response({'message': 'No hay horarios disponibles'}, status=400)

        attendances = dict()

        for schedule in schedules:
            attendances[schedule['id']] = await self.fetch_attendance_for_schedule(student_id,
                                                                                   schedule['id'],
                                                                                   self.request.app.db)

        result_data = flatten({
            'school_term': school_term,
            'schedules': schedules,
            'attendances': attendances
        }, {'with_time': True, 'long': True})

        result_data['overall'] = {}

        total_amount, attended = 0, 0

        for _i, _s in enumerate(result_data['schedules']):
            _ni = _i + 1
            result_data['overall'][_ni] = list()

            for _, _s_wa in result_data['attendances'].items():
                if _s_wa:
                    for _a in _s_wa:
                        if _a['horario_id'] == _s['id']:
                            total_amount += 1
                            if _a['asistio']:
                                attended += 1
                                result_data['overall'][_ni].append(1)
                            else:
                                result_data['overall'][_ni].append(0)

        for _k, _overall in result_data['overall'].items():
            if _overall:
                result_data['overall'][_k] = int(round(sum(_overall) / len(_overall), 2) * 100)
            else:
                result_data['overall'][_k] = 0

        if attended != 0 and total_amount != 0:
            result_data['overall']['average'] = int(round(attended / total_amount, 2) * 100)
        else:
            result_data['overall']['average'] = 0

        return json_response(result_data, status=200)


    @staticmethod
    async def fetch_attendance_for_schedule(student: int, schedule: int, dbi: PoolConnectionHolder):
        query = '''
            SELECT horario_id, fecha_registro, asistio
            FROM asistencia
            WHERE alumno_id = $1 AND
                  horario_id = $2
        '''

        async with dbi.acquire() as connection:
            return await (await connection.prepare(query)).fetch(student, schedule)

    @staticmethod
    async def fetch_schedules(school_term: int, dbi: PoolConnectionHolder):
        query = '''
            SELECT horario_profesor.id, profesor_id, nombres as profesor_nombres,
                   apellidos as profesor_apellidos, dia_clase, hora_comienzo,
                   hora_fin
            FROM horario_profesor
            LEFT JOIN usuario
                   ON usuario.id = profesor_id
            WHERE ciclo_id = $1
            ORDER BY usuario.nombres ASC, usuario.apellidos ASC, dia_clase ASC, hora_comienzo ASC
        '''
        async with dbi.acquire() as connection:
            return await (await connection.prepare(query)).fetch(school_term)

    @staticmethod
    async def fetch_school_term(school: int, dbi: PoolConnectionHolder):
        query = '''
            SELECT id, fecha_comienzo, fecha_fin
            FROM ciclo_academico
            WHERE $1 >= fecha_comienzo AND
                  $1 <= fecha_fin AND
                  escuela = $2
            LIMIT 1
        '''
        async with dbi.acquire() as connection:
            return await (await connection.prepare(query)).fetchrow(datetime.utcnow(), school)

    @staticmethod
    async def school_term_exists(school_term: int, school: int, dbi: PoolConnectionHolder):
        query = '''
            SELECT true
            FROM ciclo_academico
            WHERE id = $1 AND
                  escuela = $2
            LIMIT 1
        '''
        async with dbi.acquire() as connection:
            return await (await connection.prepare(query)).fetchval(school_term, school)


class RegisterAttendance(View):
    @view('attendance.register')
    @permission_required('registrar_asistencia')
    async def get(self, user: dict):
        students = await self.fetch_students(user['escuela'], self.request.app.db)
        can_register_attendance = await self.can_register_attendance(user['id'], user['escuela'])

        return {'students': students,
                'can_register_attendance': can_register_attendance}

    @view('attendance.register')
    @permission_required('registrar_asistencia')
    async def post(self, user: dict):
        students = await self.fetch_students(user['escuela'], self.request.app.db)
        can_register_attendance = await self.can_register_attendance(user['id'], user['escuela'])

        display_data = {
            'students': students,
            'can_register_attendance': can_register_attendance
        }

        if not can_register_attendance:
            display_data.update({'result': False})
            return display_data

        data = dict(await self.request.post())
        data = await self.convert_data(data, students)
        data = await self.format_data(data, can_register_attendance)

        result = await self.register_attendance(data, self.request.app.db)

        if isinstance(result, list):
            display_data.update({'result': True})
        else:
            display_data.update({'result': False})

        return display_data

    async def can_register_attendance(self, teacher: int, school_term: int) -> Union[bool, int]:
        async with self.request.app.db.acquire() as connection:
            schedules = await (await connection.prepare('''
                SELECT horario_profesor.dia_clase as dia, horario_profesor.hora_comienzo as comienzo,
                       horario_profesor.hora_fin as fin, horario_profesor.id
                FROM horario_profesor
                WHERE horario_profesor.ciclo_id = $1 AND
                      horario_profesor.profesor_id = $2
            ''')).fetch(school_term, teacher)

            if not schedules:
                return False

            now = datetime.utcnow() - timedelta(hours=5)

            for schedule in schedules:
                _day = parse_data_key(schedule['dia'], 'isoweekdays')

                if _day != now.weekday():
                    continue

                start = int(schedule['comienzo'] / 100), schedule['comienzo'] % 100
                end = int(schedule['fin'] / 100), schedule['fin'] % 100

                if not start[0] <= now.hour <= end[0]:
                    continue
                else:
                    if start[0] == now.hour == end[0]:
                        if start[1] <= now.minute <= end[1]:
                            if await self.check_registered_attendance(schedule['id'], start, end):
                                return False
                            else:
                                return schedule['id']
                        else:
                            continue
                    elif start[0] < now.hour < end[0]:
                        if await self.check_registered_attendance(schedule['id'], start, end):
                            return False
                        else:
                            return schedule['id']
                    else:
                        return False

            return False

    async def check_registered_attendance(self, schedule: int, start: tuple, end: tuple):
        start_ = await self.get_datetime(start)
        end_ = await self.get_datetime(end)

        async with self.request.app.db.acquire() as connection:
            return await (await connection.prepare('''
                SELECT true
                FROM asistencia
                WHERE horario_id = $1 AND
                      fecha_registro >= $2 AND
                      fecha_registro <= $3
                LIMIT 1
            ''')).fetchval(schedule, start_, end_) or False

    @staticmethod
    async def get_datetime(time_: tuple):
        now = datetime.utcnow() - timedelta(hours=5)
        return now.replace(hour=time_[0], minute=time_[1], second=0, microsecond=0)


    @staticmethod
    async def convert_data(data: dict, students: list) -> list:
        new_data = list()

        for s in students:
            data_keys = data.keys()
            attended = 'student_{}'.format(s['id'])
            additional = 'additional_{}'.format(s['id'])

            if additional not in data_keys:
                raise ValueError

            if attended not in data_keys:
                attended_bool = False
            elif data[attended] == 'on':
                attended_bool = True
            else:
                raise ValueError

            new_data.append([s['id'], data[additional], attended_bool])

        return new_data

    @staticmethod
    async def format_data(data: list, schedule: int) -> list:
        current_time = datetime.utcnow() - timedelta(hours=5)
        return [[s[0], schedule, current_time, s[1], s[2]] for s in data]

    @staticmethod
    async def register_attendance(data: list, dbi: PoolConnectionHolder):
        async with dbi.acquire() as connection:
            async with connection.transaction():
                query = '''
                    INSERT INTO asistencia (alumno_id, horario_id, fecha_registro, observacion, asistio)
                    VALUES {0}
                    RETURNING true;
                '''.format(','.join(['({})'.format(
                    ','.join(list(
                        map(lambda x: '\''+str(x)+'\'' if not isinstance(x, str) else '\''+x+'\'', v)
                    ))) for v in data]))

                return await connection.fetch(query)

    @staticmethod
    async def fetch_students(school: int,  dbi: PoolConnectionHolder):
        query = '''
            WITH ciclo_academico AS (
                SELECT ciclo_academico.id
                FROM ciclo_academico
                WHERE ciclo_academico.fecha_comienzo <= $1 AND
                      ciclo_academico.fecha_fin >= $1 AND
                      ciclo_academico.escuela = $2
                LIMIT 1
            )
            
            SELECT usuario.id, usuario.nombres, usuario.apellidos
            FROM usuario
            INNER JOIN matricula
                    ON matricula.estudiante_id = usuario.id AND
                       matricula.ciclo_acad_id = (SELECT id FROM ciclo_academico)
            WHERE usuario.rol_id = 1 AND
                  usuario.escuela = $2 AND
                  matricula.ciclo_acad_id = (SELECT id FROM ciclo_academico)
            ORDER BY apellidos ASC
        '''
        async with dbi.acquire() as connection:
            return await (await connection.prepare(query)).fetch(datetime.utcnow() - timedelta(hours=5), school)


class DetailedReport(View):
    @view('attendance.detailed_report')
    @permission_required('ver_listado_alumnos')
    async def get(self, user: dict):
        students = await self.get_students(user['escuela'])

        if students:
            students = flatten(students, {})
            school_term = students[0]['ciclo_acad_id']
            schedules = flatten(await self.fetch_schedules(school_term), {})

            for i, schedule in enumerate(schedules):
                schedules[i]['str'] = schedule_to_str(schedule['dia_clase'],
                                                      schedule['hora_comienzo'],
                                                      schedule['hora_fin'])

            del school_term

            for i, student in enumerate(students):
                students[i]['attendance'] = []
                students[i]['total_attendances'] = 0
                students[i]['total_non_attendances'] = 0
                for schedule in schedules:
                    attendances = await self.fetch_non_attendances(student['id'], schedule['id'])
                    non_attendances = attendances['inasistencias_porcentaje'] or 0
                    _non_attendances_amount = attendances['inasistencias'] or 0
                    _total_attendances = attendances['total_asistencias'] or 0

                    students[i]['total_non_attendances'] += _non_attendances_amount
                    students[i]['total_attendances'] += _total_attendances

                    students[i]['attendance'].append({
                        'attendances': 100 - non_attendances,
                        'non_attendances': non_attendances
                    })

                    del _total_attendances, _non_attendances_amount, non_attendances, attendances

            return {'students': students,
                    'schedules': schedules}

        return {'students': []}

    @staticmethod
    async def fetch_attendance_for_schedule(student: int, schedule: int, dbi: PoolConnectionHolder):
        query = '''
            SELECT horario_id, fecha_registro, asistio
            FROM asistencia
            WHERE alumno_id = $1 AND
                  horario_id = $2
        '''
        async with dbi.acquire() as connection:
            return await (await connection.prepare(query)).fetch(student, schedule)

    async def get_students(self, school: int):
        query = '''
            SELECT id, CASE WHEN tipo_documento = 0 THEN 'DNI' ELSE 'Carné de extranjería' END as tipo_documento,
                   nombres, apellidos, matricula.ciclo_acad_id
            FROM usuario
            INNER JOIN matricula
                    ON matricula.ciclo_acad_id = (SELECT id
                                                  FROM ciclo_academico
                                                  WHERE escuela = $1 AND
                                                        $2 >= fecha_comienzo AND
                                                        $2 <= fecha_fin LIMIT 1) AND
                       matricula.estudiante_id = usuario.id
        '''

        async with self.request.app.db.acquire() as connection:
            stmt = await connection.prepare(query)
            return await stmt.fetch(school, datetime.utcnow())

    async def fetch_schedules(self, school_term: int):
        query = '''
            SELECT horario_profesor.id, profesor_id, nombres as profesor_nombres,
                   apellidos as profesor_apellidos, dia_clase, hora_comienzo,
                   hora_fin
            FROM horario_profesor
            LEFT JOIN usuario
                   ON usuario.id = profesor_id
            WHERE ciclo_id = $1
            ORDER BY usuario.nombres ASC, usuario.apellidos ASC, dia_clase ASC, hora_comienzo ASC
        '''
        async with self.request.app.db.acquire() as connection:
            return await (await connection.prepare(query)).fetch(school_term)

    async def fetch_non_attendances(self, student: int, schedule: int):
        query = '''
            WITH total_asistencias AS (
                SELECT COUNT(true) as cant_asistencias
                FROM asistencia
                WHERE alumno_id = $1 AND
                      horario_id = $2
                LIMIT 1
            ), inasistencias AS (
                SELECT COUNT(true) as cant_inasistencias
                FROM asistencia
                WHERE alumno_id = $1 AND
                      horario_id = $2 AND
                      asistio = FALSE
                HAVING COUNT(*) >= 1
                LIMIT 1
            )
            SELECT CAST(
                (SELECT CAST(cant_inasistencias AS FLOAT) FROM inasistencias) /
                COALESCE((SELECT CAST(cant_asistencias AS FLOAT) FROM total_asistencias), 1) * 100
            AS INT) as inasistencias_porcentaje,
                  (SELECT cant_inasistencias FROM inasistencias) as inasistencias,
                  (SELECT cant_asistencias FROM total_asistencias) as total_asistencias
            LIMIT 1
        '''
        async with self.request.app.db.acquire() as connection:
            return await (await connection.prepare(query)).fetchrow(student, schedule)


routes = {
    'students': {
        'list': StudentsList,
        'list/school-term-{school_term:[1-9][0-9]*}': StudentsList
    },
    'attendance': {
        'register': RegisterAttendance,
        'student-report/{student_id:(?:[1-9][0-9]*|my-own)}': ReadAttendanceReport,
        'student-report/school-term-{school_term_id:[1-9][0-9]*}/{student_id:[1-9][0-9]*}': ReadAttendanceReport,
        'detailed-report': DetailedReport
    }
}
