# Образцы: плохо и лучше

Короткие примеры для разговора с разработчиками. Имена сущностей и функций условные; синтаксис своей версии сверяйте со справкой RX через rxmcp. Код в решение не вносим: это материал для задачи.

## N+1: запрос на каждой итерации

Плохо: на тысячу Id тысяча запросов.

```csharp
foreach (var id in ids)
{
  var doc = Contracts.Get(id);
  names.Add(doc.Name);
}
```

Лучше: один запрос по списку и только нужное поле.

```csharp
var names = Contracts.GetAll(c => ids.Contains(c.Id)).Select(c => c.Name).ToList();
```

Для очень длинных списков Id запрос делят на порции по несколько сотен или тысяч значений.

## ToList по большой выборке и фильтр в памяти

Плохо: вся таблица уходит в память сервиса, отбор потом.

```csharp
var overdue = Assignments.GetAll().ToList().Where(a => a.Deadline < Calendar.Now);
```

Лучше: фильтр уходит в SQL, коллекция остаётся запросом (справка RX 25.2: sds_poluchenie_bolshogo_obema_dannuh.htm).

```csharp
var overdue = Assignments.GetAll(a => a.Deadline < Calendar.Now && a.Status == Workflow.AssignmentBase.Status.InProcess);
```

## Сущность целиком ради двух полей

Справка приводит выборку в структуру вместо сущностей: `.Select(d => new DocumentInfo() { Id = d.Id, Name = d.Name }).ToList()`. SQL проще, данных меньше.

## Соединение с базой

Плохо: при исключении соединение не закроется, сборка RX 4.9+ это не пропустит.

```csharp
var connection = SQL.CreateConnection();
var command = connection.CreateCommand();
command.CommandText = "update ... where id = " + id;
command.ExecuteNonQuery();
```

Лучше: `using` для соединения и команды, значение параметром (справка: om_rabota_sql.htm, om_dobavit_parametr_k_komande_addparameter.htm).

```csharp
using (var connection = SQL.CreateConnection())
using (var command = connection.CreateCommand())
{
  command.CommandText = "update ... where id = @id";
  SQL.AddParameter(command, "@id", id, System.Data.DbType.Int64);
  command.ExecuteNonQuery();
}
```

Точную сигнатуру `AddParameter` своей версии смотрите в справке.

## Асинхронный обработчик: занятая запись, повтор, идемпотентность

Плохо: `Locks.Lock()` бросает исключение на занятой записи, обработчик уходит в системный повтор, а в логе только класс исключения; повторный запуск задвоит результат.

```csharp
public virtual void Recalc(...RecalcInvokeArgs args)
{
  var doc = Contracts.Get(args.DocumentId);
  Locks.Lock(doc);
  CreateFollowUpTask(doc);
  Locks.Unlock(doc);
}
```

Лучше: записи нет, выходим с причиной; запись занята, уходим на повтор с причиной; уже сделано, выходим; блокировка снимается в `finally` (справка: sungerodev_baseeditor_asynchandlerstab.htm, om_blokirovki.htm).

```csharp
public virtual void Recalc(...RecalcInvokeArgs args)
{
  var doc = Contracts.GetAll(d => d.Id == args.DocumentId).FirstOrDefault();
  if (doc == null)
  {
    args.RetryReason = string.Format("Recalc: no document {0}", args.DocumentId);
    return;
  }
  if (AlreadyHasFollowUpTask(doc))
    return;
  var locked = false;
  try
  {
    locked = Locks.TryLock(doc);
    if (!locked)
    {
      args.Retry = true;
      args.RetryReason = string.Format("Recalc: document {0} is locked", doc.Id);
      return;
    }
    CreateFollowUpTask(doc);
  }
  finally
  {
    if (locked)
      Locks.Unlock(doc);
  }
}
```

## Обработчик на каждое сохранение

Плохо: `AfterSave` безусловно ставит обработчик в очередь; при массовом сохранении или сохранении из другого обработчика очередь RabbitMQ разрастается.

Лучше: вызов под условием изменения нужного свойства, или перенос в фоновый процесс, или программное отключение на время миграции (справка: dds_vyzov_ao_sokhranen_kartochki.htm).

## Длинная работа: порции и отдельный Worker

Плохо: двадцать экземпляров обработчика по 10–15 минут на одном Worker, остальные обработчики и фоновые процессы стоят.

Лучше (справка: dds_zapusk_bolshogo_kolichestva_dlitelnykh_ao.htm): обработчик берёт порцию, проверяет остаток и вызывает переповтор с `NextRetryTime`, экземпляров запускается меньше, чем потоков; тяжёлые обработчики вынесены на отдельный экземпляр сервиса параметрами `ASYNC_HANDLERS_PROCESSING` и `ASYNC_HANDLERS_EXCLUDED`, число потоков `ASYNC_HANDLERS_PROCESSING_PARALLEL_COUNT`. Без требований к скорости: последовательно порциями в фоновом процессе.

## Фоновый процесс: транзакция на запись

Справка (sungerodev_baseeditor_jobstab.htm) показывает выборку списка и `Transactions.Execute()` на каждый элемент: ошибка одного не мешает остальным. В транзакцию передают Id, а не сущности; после неудачной транзакции сущности перечитывают из репозитория. Сервис не оборачивает обработчик фонового процесса в транзакцию сам.

## Вложенная транзакция

`Transactions.Execute()` нельзя вызывать там, где транзакция уже есть: события «До сохранения (в транзакции)» и «После сохранения (в транзакции)», Remote-функции, вызванные с клиента, функции инициализации, вычисления блоков схем, POST-функции сервиса интеграции (справка: om_rabota_s_tranzaktsiyami_transactions.htm). Откат применяется ко всей цепочке.

## Много Remote-вызовов с клиента

Плохо: цикл на клиенте вызывает серверную функцию для каждой строки, каждая итерация отдельный запрос к веб-серверу.

Лучше: одна серверная функция с атрибутом `Remote`, параметры структурой; функции только для чтения с `Remote(IsPure = true)` (справка: sds_obedinenie_vuchislenii.htm).

## Большая табличная часть

Табличная часть на тысячи строк гоняется между клиентом и сервером при каждом изменении карточки. Для RX 4.7 и выше справка советует не планировать больше 2000 строк и выносить такие данные в отдельный справочник (dds_razrabotka_kartochek_sbolshoi_tablichn_chast.htm).

## Индекс

Справка (sds_indeksu.htm) описывает индекс как SQL-запрос модуля, который создаётся при инициализации (в базовом решении через функцию `CreateIndexOnTable` модуля Docflow), и правила: первыми колонки из `WHERE`, уникальные лучше, имя до 63 символов, по маленьким таблицам не нужен. Как именно это оформлено в вашем решении и версии, смотрите в справке своей версии и у разработчиков.
