db.Conversations.aggregate([
 {
   $match: {
     status: 'failed',
     createdAt: { $gte: ISODate('2026-09-01T00:00:00.000Z') }
   }
 },
 {
   $lookup: {
     from: 'Sessions',
     localField: '_id',
     foreignField: 'conversation_id',
     as: 'session_info'
   }
 },
 {
   $addFields: {
     // Isolate the exact last session object
     lastSession: { $last: '$session_info' }
   }
 },
 {
   $match: {
     $expr: {
       $and: [
         // Condition 1: At least 2 sessions
         { $gte: [{ $size: '$session_info' }, 2] },

         // Condition 2: Last session status is 'failed'
         { $eq: ['$lastSession.status', 'failed'] },

         // Condition 3: Last session has NO attachments (missing, null, or empty array)
         {
           $or: [
             { $eq: [{ $type: '$lastSession.session_attachment_list' }, 'missing'] },
             { $eq: [{ $size: { $ifNull: ['$lastSession.session_attachment_list', []] } }, 0] }
           ]
         }
       ]
     }
   }
 },
 {
   $project: {
     _id: 1,
     status: 1,
     createdAt: 1,
     // Keeps ONLY the last session object as the session field
     session: '$lastSession'
   }
 },
 {
   // Saves or overwrites the results into this new collection
   $out: 'FailedConversations_LastSessionFailedNoAttachments'
 }
])