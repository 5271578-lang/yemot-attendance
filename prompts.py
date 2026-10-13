"""Everything the line says, in Hebrew, English and Yiddish. Change wording here, no code changes needed.

Notes for editing:
- A full stop (.) separates sentences. Commas are fine.
- Do not use: hyphen, double quote, single quote/apostrophe, &, |  (the phone system's speech engine rejects them).
- Words in {braces} are filled in by the system - keep them.
"""

# Yiddish is turned off: the phone system reads it with the Hebrew voice and it comes out garbled.
# To bring it back (e.g. with recorded files), add "yi" here again.
LANGUAGES = ("he", "en")

WEEKDAYS = {
    "he": ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"],
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    "yi": ["מאנטאג", "דינסטאג", "מיטוואך", "דאנערשטאג", "פרייטאג", "שבת", "זונטאג"],
}

HE = {
    "welcome": "שלום וברכה, הגעתם לקו כולל חצות, אור הצדיק, מגן אברהם",
    "enter_phone": "נא להקיש את מספר הטלפון שלכם, ובסיום סולמית",
    "enter_pin": "נא להקיש סיסמה, ובסיום סולמית",
    "bad_login": "הפרטים שהוקשו שגויים",
    "too_many": "יותר מדי ניסיונות. להתראות",
    "hello": "שלום {name}",
    "menu_main": "לעדכון כניסה הקישו 1. ליציאה חריגה הקישו 2",
    "closed_until": "עדכון הכניסה ייפתח בשעה {time}",
    "menu_driver": "לאישור יציאת ההסעה הקישו 3",
    "menu_rest": "להודעות הקישו 4. לתיקון נוכחות הקישו 5. למצב אישי הקישו 6. להגדרות אישיות הקישו 9",
    "meron_saved": "ונרשמה לך נסיעה למירון",
    "removed": "לפי כללי הכולל, לאחר חודשיים ללא מלגה נמחקת מהמערכת. לבירור נא לפנות להנהלת הכולל",
    "status_month": "בחודש {month} היית נוכח {nights} לילות, מתוכם {whole} לילות שלמים, ו {early} לילות זריזין מקדימים. אחוז הנוכחות שלך עומד על {pct} אחוז",
    "status_stipend": "המלגה שלך כרגע עומדת על {amount} שקלים",
    "status_to_percent": "כדי לסיים את החודש ב {pct} אחוז, עליך להגיע לעוד {nights} לילות שלמים לפחות, מתוך {left} הלילות שנשארו",
    "status_percent_lost": "החודש כבר לא ניתן להגיע ל {pct} אחוז",
    "status_to_minimum": "למינימום המלגה עליך להגיע לעוד {nights} לילות שלמים, מתוך {left} הלילות שנשארו",
    "status_minimum_lost": "החודש כבר לא ניתן להגיע למינימום המלגה",
    "status_meron": "זכית לנסוע למירון {nights} לילות",
    "status_driver": "הסעת {drives} פעמים",
    "status_total": "סך הכל המלגה, הכל ביחד, עומדת על {amount} שקלים",
    "invalid": "בחירה שגויה",
    "no_input": "לא התקבלה בחירה",
    "goodbye": "להתראות",
    "bad_time": "השעה שהוקשה אינה תקינה",

    # 1: entry
    "prev_exit": "היציאה שלך {when} מוגדרת לשעה {time}. לאישור הקישו 1. לשינוי, הקישו את שעת היציאה בארבע ספרות",
    "when_yesterday": "אתמול",
    "when_day": "ביום {weekday}",
    "exit_repeat": "שעת היציאה {time}. לאישור הקישו 1. להקשה מחדש הקישו 2",
    "exit_saved": "שעת היציאה נשמרה",
    "entry_saved": "הכניסה שלך עודכנה בהצלחה לשעה {time}",
    "already_in": "כבר נרשמה עבורך כניסה הלילה בשעה {time}",
    "bhz_notice": "בימי בין הזמנים מתחילים כולל בשעה 3 וחצי, וההסעה יוצאת בשעה שתיים. בין הזמנים הנוכחי ממשיך עד {date}",

    # 2: early exit
    "early_exit_saved": "היציאה החריגה נרשמה בשעה {time}",
    "not_in": "לא נמצאה כניסה הלילה. לתיקון נוכחות הקישו 5 בתפריט הראשי",

    # 3: shuttle (drivers)
    "shuttle_confirm": "לאישור יציאת ההסעה של קבוצה {group} הקישו 1. לביטול הקישו 2",
    "shuttle_no_group": "לא הוגדרה עבורך קבוצת הסעה. נא לפנות למנהל",
    "shuttle_sent": "ההודעה נשלחת ל {count} נוסעים בקבוצה {group}",
    "shuttle_nobody": "אין נוסעים עם טלפון בקבוצה זו",
    "shuttle_failed": "השליחה נכשלה. נא לפנות למנהל",
    "shuttle_call_text": "שלום {name}. ההסעה של קבוצה {group} יצאה לדרך",

    # 5: correction
    "fix_kind": "לתיקון שעת כניסה הקישו 1. לתיקון שעת יציאה הקישו 2",
    "fix_day": "לתיקון של הלילה הקישו 1. לתיקון של הלילה הקודם הקישו 2",
    "fix_time": "הקישו את השעה בארבע ספרות. לדוגמה, לשתיים וחצי הקישו 0 2 3 0",
    "fix_saved": "הבקשה לתיקון ה{kind} לשעה {time} נשמרה ותועבר לאישור המנהל",
    "fix_kind_in": "כניסה",
    "fix_kind_out": "יציאה",

    # 9: messages and personal settings
    "ext4_manager": "לשמיעת הודעות הקישו 1. להשארת הודעה הקישו 2",
    "ext4_employee": "לשמיעת הודעות הקישו 1. להשארת הודעה הקישו 2",
    "driver_send_menu": "להודעה למנהלים הקישו 1. להודעה לקבוצת ההסעה שלך ולנהגים הקישו 2",
    "switch_user": "כניסה כמשתמש אחר",
    "settings_shuttle": "לשינוי מספר השיחות בהתראת ההסעה הקישו 1",
    "settings_rest": "לשינוי הסיסמה הקישו 2. לשינוי שפה הקישו 3. לחזרה לתפריט הראשי הקישו 0",
    "menu_switch": "לכניסה כמשתמש אחר הקישו כוכבית",
    "shuttle_closed": "אישור יציאת ההסעה אפשרי רק בין השעה {start} לשעה {end}",
    "shuttle_first": "את מספר השיחות להתראה על יציאת ההסעה בוחרים עכשיו. אפשר לשנות בהמשך בשלוחה 9, בהגדרות האישיות",
    "shuttle_calls_ask": "כמה פעמים להתקשר אליך כשההסעה יוצאת. הקישו מספר בין 1 ל 5",
    "shuttle_calls_saved": "נשמר. המערכת תתקשר אליך עד {n} פעמים, ותפסיק כשתענה",
    "new_pin": "הקישו סיסמה חדשה, בין 4 ל 8 ספרות, ובסיום סולמית",
    "pin_again": "הקישו שוב את הסיסמה החדשה, ובסיום סולמית",
    "pin_mismatch": "הסיסמאות אינן זהות",
    "pin_saved": "הסיסמה שונתה בהצלחה",
    "language_menu": "לעברית הקישו 1",
    "language_saved": "השפה נשמרה",

    "send_menu_onsite": "לכל העובדים במשמרת שלך הקישו 1. לקבוצת הסעה 1 הקישו 2. "
                        "לקבוצת הסעה 2 הקישו 3. לעובד מסוים הקישו 4",
    "send_menu_tech": "לכל העובדים הקישו 1. לקבוצת הסעה 1 הקישו 2. "
                      "לקבוצת הסעה 2 הקישו 3. לעובד מסוים הקישו 4",
    "enter_target": "נא להקיש את מספר העובד, ובסיום סולמית",
    "target_not_found": "העובד לא נמצא",
    "target_not_in_shift": "העובד אינו שייך למשמרת שלך",
    "target_is": "העובד {name}",
    "no_recipients": "אין עובדים לבחירה זו",
    "record": "נא להקליט את ההודעה אחרי הצפצוף. בסיום הקישו סולמית",
    "empty_recording": "לא הוקלטה הודעה",
    "saved_to_employees": "ההודעה נשמרה ותישלח לעובדים שנבחרו",
    "saved_to_managers": "ההודעה נשמרה ונשלחה למנהלים",
    "no_messages": "אין הודעות",
    "msg_new": "הודעה חדשה",
    "msg_saved": "הודעה שמורה",
    "msg_header": "{kind}, {n} מתוך {total}, מאת {sender}, {when}",
    "when": "ביום {weekday}, {day} ב{month}, בשעה {time}",
    "after_msg": "לשמיעה חוזרת הקישו 1. להודעה הבאה הקישו 2",
    "end_messages": "אין הודעות נוספות",
    "notify_call_text": "שלום {name}. יש לך הודעה חדשה מאת {sender}. כדי לשמוע אותה, התקשרו לקו הנוכחות והקישו 4",

    # Uman (asked once, after Rosh Hashana, only from the person's own phone)
    "uman_ask": "זכית לנסוע השנה לאומן על ראש השנה. לאישור הקישו 1, אחרת הקישו 2",
    "uman_yes": "אשריך שזכית, ומי שהיה אצלי על ראש השנה ראוי לו לשמוח מאד מאד, "
                "והנסיעה של כל אחד ואחד יש לו חלק בקץ הגאולה הקרובה במהרה בימינו. אמן",
    "uman_no": "העיקר הוא הרצון, כולנו תפילה שתזכה שנה הבאה לנסוע יחד עמנו, "
               "והנסיעה של כל אחד ואחד יש לו חלק בקץ הגאולה הקרובה במהרה בימינו. אמן",
}

EN = {
    "welcome": "Hello, you have reached the line of Kollel Chatzos, Ohr Hatzadik, Magen Avraham",
    "enter_phone": "Please enter your phone number, then press pound",
    "enter_pin": "Please enter your password, then press pound",
    "bad_login": "The details you entered are incorrect",
    "too_many": "Too many attempts. Goodbye",
    "hello": "Hello {name}",
    "menu_main": "To clock in, press 1. To leave early, press 2",
    "closed_until": "Clocking in opens at {time}",
    "menu_driver": "To confirm the shuttle is leaving, press 3",
    "menu_rest": "For messages, press 4. To correct attendance, press 5. For your personal status, press 6. For personal settings, press 9",
    "meron_saved": "and a trip to Meron was recorded",
    "removed": "According to the kollel rules, after two months without a stipend you were removed from the system. Please contact the kollel office",
    "status_month": "In {month} you were present {nights} nights, of them {whole} whole nights, and {early} nights early. Your attendance is {pct} percent",
    "status_stipend": "Your stipend so far is {amount} shekels",
    "status_to_percent": "To finish the month at {pct} percent, you need at least {nights} more whole nights out of the {left} nights left",
    "status_percent_lost": "This month you can no longer reach {pct} percent",
    "status_to_minimum": "For the minimum stipend you need {nights} more whole nights out of the {left} nights left",
    "status_minimum_lost": "This month you can no longer reach the minimum stipend",
    "status_meron": "You travelled to Meron {nights} nights",
    "status_driver": "You drove the shuttle {drives} times",
    "status_total": "Your total, all together, is {amount} shekels",
    "invalid": "Invalid choice",
    "no_input": "No choice was received",
    "goodbye": "Goodbye",
    "bad_time": "The time you entered is not valid",
    "prev_exit": "Your exit {when} is set to {time}. To confirm, press 1. To change it, enter the exit time in four digits",
    "when_yesterday": "yesterday",
    "when_day": "on {weekday}",
    "exit_repeat": "Exit time {time}. To confirm, press 1. To enter it again, press 2",
    "exit_saved": "The exit time was saved",
    "entry_saved": "Your clock in was saved for {time}",
    "already_in": "You already clocked in tonight at {time}",
    "bhz_notice": "During bein hazmanim the kollel starts at three thirty, and the shuttle leaves at two. "
                  "The current bein hazmanim continues until {date}",
    "early_exit_saved": "Your early exit was recorded at {time}",
    "not_in": "No clock in was found tonight. To correct attendance, press 5 in the main menu",
    "shuttle_confirm": "To confirm that the shuttle of group {group} is leaving, press 1. To cancel, press 2",
    "shuttle_no_group": "You have no shuttle group. Please contact the manager",
    "shuttle_sent": "The message is being sent to {count} passengers in group {group}",
    "shuttle_nobody": "No passengers with a phone in this group",
    "shuttle_failed": "Sending failed. Please contact the manager",
    "shuttle_call_text": "Hello {name}. The shuttle of group {group} has left",
    "fix_kind": "To correct a clock in time, press 1. To correct an exit time, press 2",
    "fix_day": "For tonight, press 1. For the previous night, press 2",
    "fix_time": "Enter the time in four digits. For example, for two thirty, press 0 2 3 0",
    "fix_saved": "Your request to correct the {kind} to {time} was saved and will be sent to the manager",
    "fix_kind_in": "clock in",
    "fix_kind_out": "exit",
    "ext4_manager": "To hear messages, press 1. To leave a message, press 2",
    "ext4_employee": "To hear messages, press 1. To leave a message, press 2",
    "driver_send_menu": "For a message to the managers, press 1. For a message to your shuttle group and the drivers, press 2",
    "switch_user": "Sign in as another user",
    "settings_shuttle": "To change the number of shuttle alert calls, press 1",
    "settings_rest": "To change your password, press 2. To change the language, press 3. To return to the main menu, press 0",
    "menu_switch": "To sign in as another user, press star",
    "shuttle_closed": "The shuttle can be confirmed only between {start} and {end}",
    "shuttle_first": "Please choose now how many times to be called when the shuttle leaves. You can change it later by pressing 9, personal settings",
    "shuttle_calls_ask": "How many times should we call you when the shuttle leaves. Enter a number from 1 to 5",
    "shuttle_calls_saved": "Saved. We will call you up to {n} times, and stop when you answer",
    "new_pin": "Enter a new password of 4 to 8 digits, then press pound",
    "pin_again": "Enter the new password again, then press pound",
    "pin_mismatch": "The passwords do not match",
    "pin_saved": "Your password was changed",
    "language_menu": "For English, press 2",
    "language_saved": "The language was saved",
    "send_menu_onsite": "For all employees in your shift, press 1. For shuttle group 1, press 2. "
                        "For shuttle group 2, press 3. For a specific employee, press 4",
    "send_menu_tech": "For all employees, press 1. For shuttle group 1, press 2. "
                      "For shuttle group 2, press 3. For a specific employee, press 4",
    "enter_target": "Enter the employee number, then press pound",
    "target_not_found": "Employee not found",
    "target_not_in_shift": "This employee is not in your shift",
    "target_is": "Employee {name}",
    "no_recipients": "There are no employees for this choice",
    "record": "Please record your message after the beep. Press pound when you are finished",
    "empty_recording": "No message was recorded",
    "saved_to_employees": "Your message was saved and will be sent to the selected employees",
    "saved_to_managers": "Your message was saved and sent to the managers",
    "no_messages": "You have no messages",
    "msg_new": "New message",
    "msg_saved": "Saved message",
    "msg_header": "{kind} {n} of {total}, from {sender}, {when}",
    "when": "on {weekday}, {day} {month}, at {time}",
    "after_msg": "To hear the message again, press 1. For the next message, press 2",
    "end_messages": "There are no more messages",
    "notify_call_text": "Hello {name}. You have a new message from {sender}. To hear it, call the attendance line and press 4",
    "uman_ask": "Were you able to travel to Uman for Rosh Hashana this year. If yes press 1, otherwise press 2",
    "uman_yes": "Fortunate are you, and whoever was with me for Rosh Hashana should rejoice greatly, "
                "and each and every journey has a share in the coming redemption, speedily in our days. Amen",
    "uman_no": "The main thing is the will. We all pray that you merit to travel with us next year, "
               "and each and every journey has a share in the coming redemption, speedily in our days. Amen",
}

YI = {
    "welcome": "שלום וברכה, איר האט אנגעקלונגען צו דער ליניע פון כולל חצות, אור הצדיק, מגן אברהם",
    "enter_phone": "ביטע דריקט אייער טעלעפאן נומער, און צום סוף די סולמית",
    "enter_pin": "ביטע דריקט אייער פאסווארט, און צום סוף די סולמית",
    "bad_login": "די פרטים זענען נישט ריכטיג",
    "too_many": "צו פיל פרובירונגען. א גוטן טאג",
    "hello": "שלום {name}",
    "menu_main": "צו מעלדן אריינקומען דריקט 1. פאר א פריערדיגע ארויסגיין דריקט 2",
    "closed_until": "מעלדן אריינקומען עפנט זיך אום {time}",
    "menu_driver": "צו באשטעטיגן אז דער שאטל פארט ארויס דריקט 3",
    "menu_rest": "פאר מעסעדזשעס דריקט 4. צו פאררעכטן פרעזענץ דריקט 5. פאר אייער פערזענליכן מצב דריקט 6. פאר פערזענליכע איינשטעלונגען דריקט 9",
    "meron_saved": "און א פארט קיין מירון איז פארשריבן",
    "removed": "לויט די כללים פון כולל, נאך צוויי חדשים אן מלגה זענט איר אויסגעמעקט פון סיסטעם. ביטע ווענדט זיך צו די הנהלה",
    "status_month": "אין חודש {month} זענט איר געווען {nights} נעכט, דערפון {whole} גאנצע נעכט, און {early} נעכט זריזין מקדימים. אייער פרעזענץ איז {pct} פראצענט",
    "status_stipend": "אייער מלגה ביז יעצט איז {amount} שקל",
    "status_to_percent": "כדי צו ענדיגן דעם חודש מיט {pct} פראצענט, דארפט איר קומען נאך כאטש {nights} גאנצע נעכט, פון די {left} נעכט וואס בלייבן",
    "status_percent_lost": "דעם חודש קען מען שוין נישט דערגרייכן {pct} פראצענט",
    "status_to_minimum": "צום מינימום מלגה דארפט איר קומען נאך {nights} גאנצע נעכט, פון די {left} נעכט וואס בלייבן",
    "status_minimum_lost": "דעם חודש קען מען שוין נישט דערגרייכן דעם מינימום מלגה",
    "status_meron": "איר האט זוכה געווען צו פארן קיין מירון {nights} נעכט",
    "status_driver": "איר האט געפירט דעם שאטל {drives} מאל",
    "status_total": "סך הכל, אלעס צוזאמען, איז {amount} שקל",
    "invalid": "נישט ריכטיגע ברירה",
    "no_input": "קיין ברירה איז נישט אנגעקומען",
    "goodbye": "א גוטן טאג",
    "bad_time": "די צייט איז נישט ריכטיג",
    "prev_exit": "אייער ארויסגיין {when} איז געשטעלט אויף {time}. צו באשטעטיגן דריקט 1. "
                 "צו טוישן, דריקט די ארויסגיין צייט מיט פיר ציפערן",
    "when_yesterday": "נעכטן",
    "when_day": "{weekday}",
    "exit_repeat": "ארויסגיין צייט {time}. צו באשטעטיגן דריקט 1. צו דריקן נאכאמאל דריקט 2",
    "exit_saved": "די ארויסגיין צייט איז געשפייכלערט",
    "entry_saved": "אייער אריינקומען איז דערהייַנטיגט פאר {time}",
    "already_in": "איר האט שוין געמעלדן אריינקומען היינט ביינאכט אום {time}",
    "bhz_notice": "אין בין הזמנים הייבט זיך אן דער כולל אום האלב פיר, און דער שאטל פארט ארויס אום צוויי. "
                  "דער יעצטיגער בין הזמנים געדויערט ביז {date}",
    "early_exit_saved": "די פריערדיגע ארויסגיין איז פארשריבן אום {time}",
    "not_in": "מען האט נישט געטראפן קיין אריינקומען היינט ביינאכט. צו פאררעכטן דריקט 5 אין הויפט מעניו",
    "shuttle_confirm": "צו באשטעטיגן אז דער שאטל פון גרופע {group} פארט ארויס דריקט 1. צו בטל מאכן דריקט 2",
    "shuttle_no_group": "איר האט נישט קיין שאטל גרופע. ביטע ווענדט זיך צום פארוואלטער",
    "shuttle_sent": "די מעסעדזש ווערט געשיקט צו {count} פאסאזשירן אין גרופע {group}",
    "shuttle_nobody": "קיין פאסאזשירן מיט א טעלעפאן אין די גרופע",
    "shuttle_failed": "דאס שיקן איז נישט געלונגען. ביטע ווענדט זיך צום פארוואלטער",
    "shuttle_call_text": "שלום {name}. דער שאטל פון גרופע {group} איז ארויסגעפארן",
    "fix_kind": "צו פאררעכטן אריינקומען צייט דריקט 1. צו פאררעכטן ארויסגיין צייט דריקט 2",
    "fix_day": "פאר היינט ביינאכט דריקט 1. פאר די פריערדיגע נאכט דריקט 2",
    "fix_time": "דריקט די צייט מיט פיר ציפערן. למשל, פאר האלב דריי דריקט 0 2 3 0",
    "fix_saved": "די בקשה צו פאררעכטן דאס {kind} אויף {time} איז געשפייכלערט און וועט געשיקט ווערן צום פארוואלטער",
    "fix_kind_in": "אריינקומען",
    "fix_kind_out": "ארויסגיין",
    "ext4_manager": "צו הערן מעסעדזשעס דריקט 1. צו לאזן א מעסעדזש דריקט 2",
    "ext4_employee": "צו הערן מעסעדזשעס דריקט 1. צו לאזן א מעסעדזש דריקט 2",
    "driver_send_menu": "פאר א מעסעדזש צו די פארוואלטער דריקט 1. פאר א מעסעדזש צו אייער שאטל גרופע און די דרייווערס דריקט 2",
    "switch_user": "אריינגיין אלס אן אנדער באנוצער",
    "settings_shuttle": "צו טוישן וויפיל מאל מען רופט אייך ווען דער שאטל פארט ארויס דריקט 1",
    "settings_rest": "צו טוישן די פאסווארט דריקט 2. צו טוישן די שפראך דריקט 3. צוריק צום הויפט מעניו דריקט 0",
    "menu_switch": "צו אריינגיין אלס אן אנדער באנוצער דריקט שטערן",
    "shuttle_closed": "באשטעטיגן דעם שאטל איז נאר מעגליך צווישן {start} און {end}",
    "shuttle_first": "ביטע קלייבט יעצט וויפיל מאל מען זאל אייך רופן ווען דער שאטל פארט ארויס. "
                     "מען קען עס שפעטער טוישן מיט 9, אין די פערזענליכע איינשטעלונגען",
    "shuttle_calls_ask": "וויפיל מאל זאל מען אייך רופן ווען דער שאטל פארט ארויס. דריקט א נומער פון 1 ביז 5",
    "shuttle_calls_saved": "געשפייכלערט. מען וועט אייך רופן ביז {n} מאל, און אויפהערן ווען איר ענטפערט",
    "new_pin": "דריקט א נייע פאסווארט, פון 4 ביז 8 ציפערן, און צום סוף די סולמית",
    "pin_again": "דריקט נאכאמאל די נייע פאסווארט, און צום סוף די סולמית",
    "pin_mismatch": "די פאסווערטער זענען נישט גלייך",
    "pin_saved": "די פאסווארט איז געטוישט",
    "language_menu": "פאר אידיש דריקט 3",
    "language_saved": "די שפראך איז געשפייכלערט",
    "send_menu_onsite": "פאר אלע ארבעטער אין אייער שיכט דריקט 1. פאר שאטל גרופע 1 דריקט 2. "
                        "פאר שאטל גרופע 2 דריקט 3. פאר א געוויסן ארבעטער דריקט 4",
    "send_menu_tech": "פאר אלע ארבעטער דריקט 1. פאר שאטל גרופע 1 דריקט 2. "
                      "פאר שאטל גרופע 2 דריקט 3. פאר א געוויסן ארבעטער דריקט 4",
    "enter_target": "דריקט דעם ארבעטער נומער, און צום סוף די סולמית",
    "target_not_found": "דער ארבעטער איז נישט געטראפן",
    "target_not_in_shift": "דער ארבעטער איז נישט אין אייער שיכט",
    "target_is": "דער ארבעטער {name}",
    "no_recipients": "עס זענען נישטא קיין ארבעטער פאר די ברירה",
    "record": "ביטע רעקארדירט די מעסעדזש נאך דעם פיפס. צום סוף דריקט די סולמית",
    "empty_recording": "קיין מעסעדזש איז נישט רעקארדירט",
    "saved_to_employees": "די מעסעדזש איז געשפייכלערט און וועט געשיקט ווערן צו די אויסגעקליבענע",
    "saved_to_managers": "די מעסעדזש איז געשפייכלערט און געשיקט צו די פארוואלטער",
    "no_messages": "איר האט נישט קיין מעסעדזשעס",
    "msg_new": "נייע מעסעדזש",
    "msg_saved": "געשפייכלערטע מעסעדזש",
    "msg_header": "{kind}, {n} פון {total}, פון {sender}, {when}",
    "when": "{weekday}, {day} {month}, אום {time}",
    "after_msg": "צו הערן נאכאמאל דריקט 1. פאר די קומענדיגע מעסעדזש דריקט 2",
    "end_messages": "עס זענען נישטא מער מעסעדזשעס",
    "notify_call_text": "שלום {name}. איר האט א נייע מעסעדזש פון {sender}. "
                        "כדי עס צו הערן, רופט אן דעם פרעזענץ ליניע און דריקט 4",
    "uman_ask": "האט איר זוכה געווען צו פארן היי יאר קיין אומאן אויף ראש השנה. אויב יא דריקט 1, אויב נישט דריקט 2",
    "uman_yes": "אשריך שזכית, ומי שהיה אצלי על ראש השנה ראוי לו לשמוח מאד מאד, "
                "והנסיעה של כל אחד ואחד יש לו חלק בקץ הגאולה הקרובה במהרה בימינו. אמן",
    "uman_no": "העיקר הוא הרצון, מיר זענען אלע מתפלל אז איר זאלט זוכה זיין קומענדיג יאר צו פארן צוזאמען מיט אונז, "
               "והנסיעה של כל אחד ואחד יש לו חלק בקץ הגאולה הקרובה במהרה בימינו. אמן",
}

P_ALL = {"he": HE, "en": EN, "yi": YI}


def text(lang, key, **kw):
    table = P_ALL.get(lang) or HE
    return table.get(key, HE[key]).format(**kw)


def say_time(dt, lang="he"):
    h, m = dt.hour, dt.minute
    if lang == "en":
        return f"{h}" if m == 0 else f"{h} {m:02d}"
    if lang == "yi":
        return f"{h}" if m == 0 else f"{h} און {m} מינוט"
    return f"{h}" if m == 0 else f"{h} ו {m} דקות"


# Kept for old imports
P = HE
