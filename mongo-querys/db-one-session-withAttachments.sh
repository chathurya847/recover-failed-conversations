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
    $match: {
      // Condition 1: Exactly 1 session in session_info array
      $expr: { $eq: [{ $size: '$session_info' }, 1] },
      
      // Condition 2: That single session has at least 1 attachment
      'session_info.0.session_attachment_list.0': { $exists: true }
    }
  },
  {
    $project: {
      _id: 1,
      status: 1,
      createdAt: 1,
      sessions: '$session_info'
    }
  },
  {
    $out: 'FailedConversationsWithAttachments_OneSession'
  }
])